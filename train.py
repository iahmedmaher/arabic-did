from comet_ml import Experiment
from torch.nn.functional import cross_entropy
from torch.utils.data import DataLoader
import torch.optim as optim
import torch.nn as nn
import os
import data
from preprocessing import TextPreprocessor
import config as cfg
import models as m
import torch
import collections
from eval import Evaluation
import numpy as np
import random
from helpers import get_datasets_paths
from colorama import Fore
from colorama import Style

train_dataset = None
eval_dataset = None


def flatten_dict(d, parent_key='', sep='_'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def get_comet_api_key(config):
    path = config['comet_api_key_file_path']
    with open(path, 'r') as f:
        return f.readline().rstrip()


def get_train_dataloader(config, transformer):
    global train_dataset

    if train_dataset is None:
        train_paths = get_datasets_paths(config, 'train')
        train_dataset = data.CSVDatasetsMerger(train_paths)

    return DataLoader(train_dataset,
                      batch_size=config['training']['train_batch_size'],
                      shuffle=True,
                      drop_last=False,
                      num_workers=config['training']['n_train_workers'],
                      collate_fn=transformer)


def get_eval_dataloader(config, transformer):
    global eval_dataset

    if eval_dataset is None:
        eval_paths = get_datasets_paths(config, 'test')
        eval_dataset = data.CSVDatasetsMerger(eval_paths)

    return DataLoader(eval_dataset,
                      batch_size=config['evaluation']['eval_batch_size'],
                      shuffle=False,
                      drop_last=False,
                      num_workers=config['evaluation']['n_eval_workers'],
                      collate_fn=transformer)


def get_optimizers(model, config):
    if config['optimizer']['name'] == 'adam':
        non_sparse = optim.Adam(model.get_non_sparse_parameters(), lr=config['optimizer']['lr'],
                                betas=config['optimizer']['betas'],
                                eps=config['optimizer']['eps'])
        sparse_params = model.get_sparse_parameters()
        if len(list(sparse_params)) == 0:
            return [non_sparse]
        else:
            sparse = optim.SparseAdam(model.get_sparse_parameters(), lr=config['optimizer']['lr'],
                                      betas=config['optimizer']['betas'],
                                      eps=config['optimizer']['eps'])
            return non_sparse, sparse
    else:
        raise NotImplementedError()


def setup_training(config):
    experiment = Experiment(get_comet_api_key(config), project_name=config['comet_project_name'], log_code=True)
    experiment.log_parameters(flatten_dict(config))

    train_text_proc = TextPreprocessor(config)
    eval_text_proc = TextPreprocessor(config, return_text=True)

    train_dataloader = get_train_dataloader(config, train_text_proc)
    eval_dataloader = get_eval_dataloader(config, eval_text_proc)

    model = m.get_model(train_text_proc.n_tokens, config)

    loss_func = nn.CrossEntropyLoss()
    return experiment, model, train_dataloader, eval_dataloader, loss_func


def normal_training(config):
    device = torch.device(config['device'])
    print('Using device', device)
    exp, model, train_dataloader, eval_dataloader, loss_func = setup_training(config)
    exp.set_name(config['experiment_name'])
    model.train()
    model = model.to(device)
    optimizers = get_optimizers(model, config)
    evaluator = Evaluation(eval_dataloader, config)

    num_examples = 0
    for epoch in range(config['training']['training_epochs']):
        for idx, batch in enumerate(train_dataloader):
            batch = (batch[0].to(device), batch[1].to(device))
            num_examples += len(batch[0])
            loss, train_accuracy = training_step(batch, model, optimizers, loss_func)
            if idx % config['training']['log_every_n_batches'] == 0:
                print(epoch, num_examples, loss.detach().cpu().numpy())
                exp.log_metric('train_loss', loss.detach().cpu().numpy(), step=num_examples, epoch=epoch)

            if idx % config['training']['eval_every_n_batches'] == 0:
                results = evaluator.eval_model(model, loss_func)
                for metric in results:
                    print(metric, results[metric])
                    exp.log_metric(metric, results[metric], step=num_examples, epoch=epoch)


def training_step(training_batch, model, optimizers, loss_func):
    model.train()
    [opt.zero_grad() for opt in optimizers]
    labels, tokens = training_batch
    outputs = model(tokens)
    chosen_label = torch.argmax(outputs[-1, :, :], dim=1)
    train_accuracy = chosen_label[chosen_label == labels].nelement()/labels.nelement()
    output_len, classes = outputs.size()[0], outputs.size()[2]
    outputs = outputs.view(-1, classes)
    labels = labels.repeat(output_len)
    loss = loss_func(outputs, labels)
    loss.backward()
    [opt.step() for opt in optimizers]
    return loss, train_accuracy


def tune_training(config):
    global global_dict
    from hyper_tune import TuneTrainable
    import ray
    from ray import tune
    from ray.tune.schedulers import HyperBandScheduler, HyperBandForBOHB, AsyncHyperBandScheduler, FIFOScheduler
    import ray.tune.suggest as suggest
    from ray.tune import sample_from, Experiment
    import ConfigSpace as CS
    import ConfigSpace.hyperparameters as CSH
    from ray.tune.suggest.hyperopt import HyperOptSearch
    from hyperopt import hp

    ray.init()
    stop_dict = {'num_examples': config['tune']['max_t'], 'no_change_in_accu' : 2} # TODO
    if config['tune']['tuning_method'] == 'bohb':
        config_space = CS.ConfigurationSpace(seed=42)

        # replace | convention is a kludge because of BOHB's specialized interface
        config_space.add_hyperparameters([CSH.UniformIntegerHyperparameter('replace|num_layers', lower=1, upper=5),
                                          CSH.UniformIntegerHyperparameter('replace|hidden_size', lower=64, upper=512),
                                          CSH.UniformIntegerHyperparameter('replace|embedding_size', lower=64,
                                                                           upper=512),
                                          CSH.UniformFloatHyperparameter('replace|dropout', lower=0.0, upper=0.5),
                                          CSH.CategoricalHyperparameter('replace|bidirectional', choices=[True, False]),
                                          CSH.UniformFloatHyperparameter('replace|lr', lower=0.00001, upper=0.1,
                                                                         log=True)])
        bohb_hyperband = HyperBandForBOHB(time_attr='num_examples', metric=config['tune']['discriminating_metric'],
                                          mode=config['tune']['discriminating_metric_mode'],
                                          max_t=config['tune']['max_t'])

        bohb_search = suggest.bohb.TuneBOHB(config_space, max_concurrent=1,
                                            metric=config['tune']['discriminating_metric'],
                                            mode=config['tune']['discriminating_metric_mode'])

        return tune.run(TuneTrainable, config=config, scheduler=bohb_hyperband, search_alg=bohb_search, num_samples=1,
                        name=config['experiment_name'],
                        resources_per_trial=config['tune']['resources_per_trial'],
                        local_dir=config['tune']['working_dir'])

    elif config['tune']['tuning_method'] == 'hyperopt':
        class HyperOptFIFO(FIFOScheduler):
            def on_trial_complete(self, trial_runner, trial, result):
                algo.save(hyper_opt_checkpoint_dir)
                print(f'{Fore.GREEN} Checkpointing hyperopt ...{Style.RESET_ALL}')

                return super().on_trial_complete(trial_runner, trial, result)

        space = {"allocate|hidden_size": hp.quniform("hidden_size", 32, 700, 2),
                 "allocate|embedding_size": hp.quniform("embedding_size", 32, 700, 2),
                 "allocate|bidirectional": hp.choice("bidirectional", [True, False]),
                 "allocate|num_layers": hp.quniform("num_layers", 1, 5, 1),
                 "allocate|penalize_all_steps": hp.choice("penalize_all_steps", [True, False])
                 }

        algo = HyperOptSearch(space, max_concurrent=1, metric=config['tune']['discriminating_metric'],
                              mode=config['tune']['discriminating_metric_mode'], n_initial_points=7,
                              random_state_seed=42)

        hyper_opt_checkpoint_dir = os.path.join(config['tune']['working_dir'], config['experiment_name'], 'hyperopt')
        if config['tune']['resume']:
            try:
                algo.restore(hyper_opt_checkpoint_dir)
                n_trials = len(algo._hpopt_trials)
                print(f"{Fore.GREEN}{n_trials} trials loaded. Warm starting ...{Style.RESET_ALL}")

            except:
                print(f'{Fore.RED}Unable to load trials. Cold starting ...{Style.RESET_ALL}')

        tune.run(TuneTrainable, config=config, search_alg=algo, num_samples=config['tune']['n_samples'],
                 scheduler=HyperOptFIFO(),
                 name=config['experiment_name'], resume=False, checkpoint_at_end=False,
                 resources_per_trial=config['tune']['resources_per_trial'],
                 local_dir=config['tune']['working_dir'], stop=stop_dict)

    elif config['tune']['tuning_method'] == 'no_search':
        tune.run(TuneTrainable, config=config, num_samples=config['tune']['n_samples'],
                 name=config['experiment_name'], resume=False, checkpoint_at_end=False,
                 resources_per_trial=config['tune']['resources_per_trial'],
                 local_dir=config['tune']['working_dir'], stop=stop_dict)

    else:
        raise NotImplementedError()


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    analysis = tune_training(cfg.default_config)
