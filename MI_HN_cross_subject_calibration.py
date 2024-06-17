'''
HN cross-subject calibration experiment: hold out each person as the new arrival, and pre-train
the HyperBCI on everyone else put together as the pre-train pool. For the new arrival person, 
their data is splitted into calibration set and validation set. Varying amount of data is drawn
from the calibration set for calibration, then the calibrated model is evaluated using the test set.

The calibration process is unsupervised; the HN is expected to pick up relevant info from the
calibration set.
'''

import matplotlib.pyplot as plt
from braindecode.datasets import MOABBDataset, BaseConcatDataset
from numpy import multiply
from braindecode.preprocessing import (Preprocessor,
                                       exponential_moving_standardize,
                                       preprocess)
from braindecode.preprocessing import create_windows_from_events
import torch
from braindecode.util import set_random_seeds
import pandas as pd
from scipy import stats
import os
import pickle
import numpy as np

from torch.utils.data import DataLoader

from utils import (
    get_subset, import_model, parse_training_config, 
    train_one_epoch, test_model
)
from models.HypernetBCI import HyperBCINet

import warnings
warnings.filterwarnings('ignore')

### ----------------------------- Experiment parameters -----------------------------
args = parse_training_config()
model_object = import_model(args.model_name)
# subject_ids_lst = list(range(1, 14))
subject_ids_lst = [1, 2,]
dataset = MOABBDataset(dataset_name=args.dataset_name, subject_ids=subject_ids_lst)

print('Data loaded')

results_file_name = f'HYPER{args.model_name}_{args.dataset_name}_xsubj_calib_{args.experiment_version}'
dir_results = 'results/'
# used to store pre-trained model parameters
temp_exp_name = f'HN_xsubj_calibration_{args.experiment_version}_pretrain'
file_path = os.path.join(dir_results, f'{results_file_name}.pkl')
print(f'Saving results at {file_path}')

### ----------------------------- Plotting parameters -----------------------------
match args.data_amount_unit:
    case 'trial':
        unit_multiplier = 1
    case 'sec':
        unit_multiplier = args.trial_len_sec
    case 'min':
        unit_multiplier = args.trial_len_sec / 60
    case _:
        unit_multiplier = args.trial_len_sec

### ----------------------------- Preprocessing -----------------------------
low_cut_hz = 4.  
high_cut_hz = 38. 
# Parameters for exponential moving standardization
factor_new = 1e-3
init_block_size = 1000
# Factor to convert from V to uV
factor = 1e6

preprocessors = [
    # Keep EEG sensors
    Preprocessor('pick_types', eeg=True, meg=False, stim=False),  
    # Convert from V to uV
    Preprocessor(lambda data: multiply(data, factor)), 
    # Bandpass filter
    Preprocessor('filter', l_freq=low_cut_hz, h_freq=high_cut_hz),  
    # Exponential moving standardization
    Preprocessor(exponential_moving_standardize,  
                factor_new=factor_new, init_block_size=init_block_size)
]

# Transform the data
preprocess(dataset, preprocessors, n_jobs=-1)

### ----------------------------- Extract trial windows -----------------------------
trial_start_offset_seconds = -0.5
# Extract sampling frequency, check that they are same in all datasets
sfreq = dataset.datasets[0].raw.info['sfreq']
assert all([ds.raw.info['sfreq'] == sfreq for ds in dataset.datasets])
# Calculate the trial start offset in samples.
trial_start_offset_samples = int(trial_start_offset_seconds * sfreq)

# Create windows using braindecode function for this. It needs parameters to define how
# trials should be used.
windows_dataset = create_windows_from_events(
    dataset,
    trial_start_offset_samples=trial_start_offset_samples,
    trial_stop_offset_samples=0,
    preload=True,
)

### ----------------------------- Create model -----------------------------
# Specify which GPU to run on to avoid collisions
os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_number

cuda = torch.cuda.is_available() 
if cuda:
    print('CUDA available, use GPU for training')
    torch.backends.cudnn.benchmark = True
    device = 'cuda'
else:
    print('No CUDA available, use CPU for training')
    device = 'cpu'

seed = 20200220
set_random_seeds(seed=seed, cuda=cuda)

### ----------------------------- Training -----------------------------

classes = list(range(args.n_classes))
n_chans = windows_dataset[0][0].shape[0]
input_window_samples = windows_dataset[0][0].shape[1]

splitted_by_subj = windows_dataset.split('subject')

dict_results = {}
results_columns = ['valid_accuracy',]

for holdout_subj_id in subject_ids_lst:
    
    print(f'Hold out data from subject {holdout_subj_id}')
    
    ### ---------- Split dataset into pre-train set and fine-tune (holdout) set ----------
    pre_train_set = BaseConcatDataset([splitted_by_subj.get(f'{i}') for i in subject_ids_lst if i != holdout_subj_id])
    calibrate_set = BaseConcatDataset([splitted_by_subj.get(f'{holdout_subj_id}'),])

    ### -----------------------------------------------------------------------------------------
    ### ---------------------------------------- PRETRAINING ------------------------------------
    ### -----------------------------------------------------------------------------------------

    # Check if a pre-trained model exists
    # shouldn't have har coded it. Need to think of a better way to use pretrained models from other experiment
    # temp_exp_name = 'baseline_2_6_pretrain'

    model_param_path = os.path.join(dir_results, f'{temp_exp_name}_without_subj_{holdout_subj_id}_model_params.pth')
    model_exist = os.path.exists(model_param_path) and os.path.getsize(model_param_path) > 0

    if model_exist:
        if args.only_pretrain:
            continue
    else:
        ### ---------------------------- CREATE PRIMARY NETWORK ----------------------------
        cur_model = model_object(
            n_chans,
            args.n_classes,
            input_window_samples=input_window_samples,
            **(args.model_kwargs)
        )
                    
        ### ----------------------------------- CREATE HYPERNET BCI -----------------------------------
        # embedding length = 729 when conv1d kernel size = 5, stide = 3, input_window_samples = 2250
        embedding_shape = torch.Size([1, 749])
        sample_shape = torch.Size([n_chans, input_window_samples])
        pretrain_HNBCI = HyperBCINet(cur_model, embedding_shape, sample_shape)
        # Send to GPU
        if cuda:
            cur_model.cuda()
            pretrain_HNBCI.cuda()

        optimizer = torch.optim.AdamW(
            pretrain_HNBCI.parameters(),
            lr=args.lr, 
            weight_decay=args.weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer,
            T_max=args.n_epochs - 1
        )
        loss_fn = torch.nn.NLLLoss()

        ### ---------------------------- PREPARE PRETRAIN DATASETS ----------------------------
        ### THIS PART IS FOR BCNI2014001
        if args.dataset_name == 'BCNI2014001':
            pre_train_train_set_lst = []
            pre_train_test_set_lst = []
            pre_train_test_set_size = 1 # runs
            for key, val in pre_train_set.split('subject').items():
                subj_splitted_lst_by_run = list(val.split('run').values())
                pre_train_train_set_lst.extend(subj_splitted_lst_by_run[:-pre_train_test_set_size])
                pre_train_test_set_lst.extend(subj_splitted_lst_by_run[-pre_train_test_set_size:])
        
        ### THIS PART IS FOR SHCIRRMEISTER 2017
        elif args.dataset_name == 'Schirrmeister2017':
            pre_train_train_set_lst = []
            pre_train_test_set_lst = []
            for key, val in pre_train_set.split('subject').items():
                # print(f'Splitting data of subject {key}')
                subj_splitted_by_run = val.split('run')

                cur_train_set = subj_splitted_by_run.get('0train')
                # pre_train_train_set_lst.extend(cur_train_set)
                pre_train_train_set_lst.append(cur_train_set)

                cur_test_set = subj_splitted_by_run.get('1test')
                # pre_train_test_set_lst.extend(cur_test_set)
                pre_train_test_set_lst.append(cur_test_set)
        
        pre_train_train_set = BaseConcatDataset(pre_train_train_set_lst)
        pre_train_test_set = BaseConcatDataset(pre_train_test_set_lst)
        pre_train_train_loader = DataLoader(pre_train_train_set, batch_size=args.batch_size, shuffle=True)
        pre_train_test_loader = DataLoader(pre_train_test_set, batch_size=args.batch_size)

        # test_accuracy_lst = []
        for epoch in range(1, args.n_epochs + 1):
            print(f"Epoch {epoch}/{args.n_epochs}: ", end="")

            train_loss, train_accuracy = train_one_epoch(
                pre_train_train_loader, 
                pretrain_HNBCI, 
                loss_fn, 
                optimizer, 
                scheduler, 
                epoch, 
                device,
                print_batch_stats=False
            )
            
            # Update weight tensor for each evaluation pass
            pretrain_HNBCI.calibrate()
            test_loss, test_accuracy = test_model(
                pre_train_test_loader, 
                pretrain_HNBCI, 
                loss_fn
            )
            pretrain_HNBCI.calibrating = False

            print(
                f"Train Accuracy: {100 * train_accuracy:.2f}%, "
                f"Average Train Loss: {train_loss:.6f}, "
                f"Test Accuracy: {100 * test_accuracy:.1f}%, "
                f"Average Test Loss: {test_loss:.6f}\n"
            )

        # Save the model parameters to a file
        torch.save(
            {
                'HN_params_dict': pretrain_HNBCI.state_dict(), 
                'primary_params': pretrain_HNBCI.primary_params
            },
            model_param_path
        )

    ### -----------------------------------------------------------------------------------------
    ### ---------------------------------------- CALIBRATION ------------------------------------
    ### -----------------------------------------------------------------------------------------

    ### ----------------------------------- PREPARE CALIBRATION DATASETS -----------------------------------
    ### THIS PART IS FOR BCNI2014001
    if args.dataset_name == 'BCNI2014001':
        calibrate_splitted_lst_by_run = list(calibrate_set.split('run').values())
        subj_calibrate_set = BaseConcatDataset(calibrate_splitted_lst_by_run[:-1])
        subj_valid_set = BaseConcatDataset(calibrate_splitted_lst_by_run[-1:])
    ### THIS PART IS FOR SHCIRRMEISTER 2017
    elif args.dataset_name == 'Schirrmeister2017':
        calibrate_splitted_lst_by_run = calibrate_set.split('run')
        subj_calibrate_set = calibrate_splitted_lst_by_run.get('0train')
        subj_valid_set = calibrate_splitted_lst_by_run.get('1test')

    ### Resume pretrained model
    calibrate_model = model_object(
        n_chans,
        args.n_classes,
        input_window_samples=input_window_samples,
        **(args.model_kwargs)
    )
    calibrate_HNBCI = HyperBCINet(calibrate_model, embedding_shape, sample_shape)
    pretrained_params = torch.load(model_param_path)
    calibrate_HNBCI.load_state_dict(pretrained_params['HN_params_dict'])
    calibrate_HNBCI.primary_params = pretrained_params['primary_params']
    # Send to GPU
    if cuda:
        calibrate_model.cuda()
        calibrate_HNBCI.cuda()

    ### Calculate baseline accuracy of the uncalibrated model on the calibrate_valid set
    subj_valid_loader = DataLoader(subj_valid_set, batch_size=args.batch_size)
    _, calibrate_baseline_acc = test_model(subj_valid_loader, calibrate_HNBCI, loss_fn)
    print(f'Before calibrating for subject {holdout_subj_id}, the baseline accuracy is {calibrate_baseline_acc}')

    ### Calibrate with varying amount of new data
    dict_subj_results = {0: [calibrate_baseline_acc,]}
    calibrate_trials_num = len(subj_calibrate_set.get_metadata())
    for calibrate_data_amount in np.arange(1, (calibrate_trials_num // args.data_amount_step) + 1) * args.data_amount_step:

        test_accuracy_lst = []
        
        ### Since we're sampling randomly, repeat for 'repetition' times
        for i in range(args.repetition):

            ## Get current calibration samples
            subj_calibrate_subset = get_subset(
                subj_calibrate_set, 
                int(calibrate_data_amount), 
                random_sample=True
            )

            # Restore to the pre-trained state
            calibrate_HNBCI.load_state_dict(pretrained_params['HN_params_dict'])
            calibrate_HNBCI.primary_params = pretrained_params['primary_params']
            # Send to GPU
            if cuda:
                calibrate_model.cuda()
                calibrate_HNBCI.cuda()
    
            ### CALIBRATE! PASS IN THE ENTIRE SUBSET
            print(f'Calibrating model for subject {holdout_subj_id}' +
                  f'with {len(subj_calibrate_subset)} trials (repetition {i})'
            )

            # This dataloader returns the whole subset at once.
            subj_calibrate_loader = DataLoader(
                subj_calibrate_subset, 
                batch_size=len(subj_calibrate_subset), 
                shuffle=True
            )
            calibrate_HNBCI.calibrate()
            _, _ = test_model(subj_calibrate_loader, calibrate_HNBCI, loss_fn)
            calibrate_HNBCI.calibrating = False

            # Test the calibrated model
            test_loss, test_accuracy = test_model(subj_valid_loader, calibrate_HNBCI, loss_fn)
            test_accuracy_lst.append(test_accuracy)

            print(
                f"Test Accuracy: {100 * test_accuracy:.1f}%, "
                f"Average Test Loss: {test_loss:.6f}\n"
            )
        
        dict_subj_results.update(
            {
                calibrate_data_amount: test_accuracy_lst
            }
        )

    dict_results.update(
        {
            holdout_subj_id: dict_subj_results
        }
    )
    ### ----------------------------- Save results -----------------------------
    # Save results after done with a subject, in case server crashes
    # remove existing results file if one exists
    if os.path.exists(file_path):
        os.remove(file_path)
    # save the updated one
    with open(file_path, 'wb') as f:
        pickle.dump(dict_results, f)

# check if results are saved correctly
if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
    with open(file_path, 'rb') as f:
        dummy = pickle.load(f)
    print("Data was saved successfully.")
else:
    print(f"Error: File '{file_path}' does not exist or is empty. The save was insuccesful")

### -----------------------------------------------------------------------------------------
### ---------------------------------------- PLOTTING ---------------------------------------
### -----------------------------------------------------------------------------------------
df_results = pd.DataFrame(dict_results)
fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(20, 6))

for col in df_results.columns:
    y_values = [np.mean(lst) for lst in df_results[col]]
    y_errors = [np.std(lst) for lst in df_results[col]]
    ax1.errorbar(
        df_results.index * unit_multiplier, 
        y_values, 
        yerr=y_errors, 
        label=f'Subject {col}'
    )
    ax2.plot(
        df_results.index * unit_multiplier, 
        y_values, 
        label=f'Subject {col}'
    )

df_results_rep_avg = df_results.applymap(lambda x: np.mean(x))
subject_averaged_df = df_results_rep_avg.mean(axis=1)
std_err_df = df_results_rep_avg.sem(axis=1)
conf_interval_df = stats.t.interval(
    args.significance_level, 
    len(df_results_rep_avg.columns) - 1, 
    loc=subject_averaged_df, 
    scale=std_err_df
)

ax3.plot(
    subject_averaged_df.index * unit_multiplier, 
    subject_averaged_df, 
    label='Subject averaged'
)
ax3.fill_between(
    subject_averaged_df.index * unit_multiplier, 
    conf_interval_df[0], 
    conf_interval_df[1], 
    color='b', 
    alpha=0.3, 
    label=f'{args.significance_level*100}% CI'
)

for ax in [ax1, ax2, ax3]:
    ax.legend()
    ax.set_xlabel(f'Calibration data amount ({args.data_amount_unit})')
    ax.set_ylabel('Accuracy')

plt.suptitle(
    f'HYPER{args.model_name} on {args.dataset_name} Dataset \n , ' +
    'Calibrate model for each subject (cross subject calibration), ' +
    f'{args.repetition} reps each point'
)
plt.savefig(os.path.join(dir_results, f'{results_file_name}.png'))