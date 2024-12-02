# %%
import numpy as np

from braindecode.samplers import SequenceSampler

from sklearn.utils import check_random_state
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

from skorch.dataset import Dataset

from monge_alignment.utils import MongeAlignment
import matplotlib.pyplot as plt
from typing import Iterable

import torch
from torch import nn
import torch.fft

# import DAtaloader
from tqdm import tqdm

import pandas as pd

device = "cuda:1" if torch.cuda.is_available() else "cpu"
torch.set_num_threads(1)


class SequenceDataset(Dataset):
    def __init__(self, X, y, subject_ids, domains, target_transform=None):
        super().__init__(X=X, y=y)
        self.domains = domains
        self.subject_ids = subject_ids
        self.target_transform = target_transform
        self.create_metadata()

    def create_metadata(self):
        self.metadata = pd.DataFrame(
            {
                "target": self.y,
                "subject": self.subject_ids,
                "run": self.domains,
                "i_window_in_trial": np.arange(len(self.y)),
                "i_start_in_trial": np.zeros(len(self.y)),
                "i_stop_in_trial": 3000 * np.ones(len(self.y)),
            }
        )

    def __len__(self):
        return len(self.X)

    def _get_sequence(self, indices):
        X, y = list(), list()
        for ind in indices:
            out_i = super().__getitem__(ind)
            X.append(out_i[0])
            y.append(out_i[1])

        X = np.stack(X, axis=0)
        y = np.array(y)

        return X, y

    def __getitem__(self, idx):
        if isinstance(idx, Iterable):  # Sample multiple windows
            item = self._get_sequence(idx)
        else:
            item = super().__getitem__(idx)
        if self.target_transform is not None:
            item = item[:1] + (self.target_transform(item[1]),) + item[2:]

        return item


def accuracy_multi(model, X, y):
    y_pred = model.predict(X)
    # y_pred = y_pred[:, 12:24]
    # y = y[:, 12:24]
    return accuracy_score(y.flatten(), y_pred.flatten())


def welch_psd(signal, fs=1.0, nperseg=None, noverlap=None, window="hamming", axis=-1):
    """
    Compute the Power Spectral Density (PSD) of a signal using Welch's method along a specified axis.

    Parameters:
    - signal (torch.Tensor): Tensor of the input signal (can be multi-dimensional).
    - fs (float): Sampling frequency of the signal.
    - nperseg (int): Length of each segment.
    - noverlap (int): Number of points to overlap between segments.
    - window (str): Window function to apply on each segment.
    - axis (int): Axis along which to compute the PSD.

    Returns:
    - freqs (torch.Tensor): Array of sample frequencies.
    - psd (torch.Tensor): Power spectral density of each frequency component along the specified axis.
    """
    if nperseg is None:
        nperseg = 256
    if noverlap is None:
        noverlap = nperseg // 2
    # Move the specified axis to the last dimension for easier processing
    signal = signal.transpose(axis, -1)

    # Define the window function
    if window == "hamming":
        window_vals = torch.hamming_window(
            nperseg, periodic=False, device=signal.device
        )
    elif window == "hann":
        window_vals = torch.hann_window(nperseg, periodic=False, device=signal.device)
    elif window is None:
        window_vals = torch.ones(nperseg, device=signal.device)
    else:
        raise ValueError("Unsupported window type")

    scaling = (window_vals * window_vals).sum()
    # Calculate step size and number of segments along the last axis
    step = nperseg - noverlap
    num_segments = (signal.shape[-1] - noverlap) // step

    # Pre-allocate array for the PSD, retaining all other dimensions
    psd_sum = torch.zeros(*signal.shape[:-1], nperseg // 2 + 1, device=signal.device)

    # Iterate over segments along the last axis
    for i in range(num_segments):
        # Extract the segment
        segment = signal[..., i * step : i * step + nperseg]

        # detrend
        segment = segment - torch.mean(segment, axis=-1, keepdim=True)

        # Apply window function
        windowed_segment = segment * window_vals
        # Compute the FFT and PSD for the segment along the last axis
        segment_fft = torch.fft.rfft(windowed_segment, dim=-1)
        segment_psd = torch.abs(segment_fft) ** 2 / (fs * scaling)
        if nperseg % 2:
            segment_psd[..., 1:] *= 2
        else:
            segment_psd[..., 1:-1] *= 2
        # Accumulate PSDs from each segment
        psd_sum += segment_psd

    # Average PSD over all segments
    psd = psd_sum / num_segments

    # Compute frequency axis
    freqs = torch.fft.rfftfreq(nperseg, d=1 / fs)

    # Reshape PSD to match the original dimensions with the last axis replaced by frequency components
    return freqs, psd.transpose(axis, -1)


class TMANorm(nn.Module):
    def __init__(self, filter_size, momentum=0.1):
        super(TMANorm, self).__init__()
        self.filter_size = filter_size
        self.momentum = momentum
        self.register_buffer("running_barycenter", torch.zeros(1))
        self.first_iter = True

    def forward(self, x):
        # x: (B, C, T)

        # compute psd for each channel using welch method
        # psd: (B, C, F)
        psd = welch_psd(x, window=None, nperseg=self.filter_size)[1]

        # compute running barycenter of psd
        # barycenter: (C, F,)
        weights = torch.ones_like(psd) / psd.shape[-1]
        new_barycenter = torch.sum(weights * torch.sqrt(psd), axis=0) ** 2

        # update running barycenter
        if self.first_iter:
            self.running_barycenter = new_barycenter
            self.first_iter = False
        else:
            self.running_barycenter = (
                1 - self.momentum
            ) * self.running_barycenter + self.momentum * new_barycenter

        # compute filter
        # H: (B, C, F)
        D = torch.sqrt(self.running_barycenter) / torch.sqrt(psd)
        H = torch.fft.irfft(D, dim=-1)
        H = torch.fft.fftshift(H, dim=-1)

        # apply filter, convolute H with x
        # x_filtered: (B, C, T)
        H = torch.flip(H, dims=[-1])
        n_chan = x.shape[1]
        n_batch = x.shape[0]
        x_filtered = torch.cat(
            [
                torch.nn.functional.conv1d(
                    x[i : i + 1],
                    H[i : i + 1].view(n_chan, 1, -1),
                    padding="same",
                    groups=n_chan,
                )
                for i in range(n_batch)
            ]
        )

        return x_filtered


# %%
dataset_names = [
    "ABC",
    "CHAT",
    "CFS",
    "SHHS",
    # "HOMEPAP",
    # "CCSHS",
    # "MASS",
    # "Physionet",
    # "SOF",
    # "MROS",
]

data_dict = {}
max_subjects = 2
for dataset_name in dataset_names:
    subject_ids_ = np.load(f"data/{dataset_name}/subject_ids.npy")
    X_ = []
    y_ = []
    subject_selected = []
    for subject in tqdm(subject_ids_):
        X_.append(np.load(f"data/{dataset_name}/X_{subject}.npy"))
        y_.append(np.load(f"data/{dataset_name}/y_{subject}.npy"))
        subject_selected.append(subject)
        if len(X_) == max_subjects:
            break
    data_dict[dataset_name] = [X_, y_, subject_selected]


# %%
module_name = "usleep"
n_windows = 35
n_windows_stride = 10
max_epochs = 300
batch_size = 64
patience = 30
n_jobs = 1
seed = 42
lr = 1e-3
weight = "unbalanced"
use_scheduler = False

scaling = "None"
results_path = (
    f"results/pickle/results_usleep_{scaling}_"
    f"{len(dataset_names)}_dataset_with_{max_subjects}"
    f"_subjects_scheduler_{use_scheduler}_lr_{lr}.pkl"
)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
rng = check_random_state(seed)
dataset_target = "ABC"
# for dataset_target in dataset_names:
# X_target, y_target, subject_ids_target = data_dict[dataset_target]
X_train, X_val, y_train, y_val = (
    [],
    [],
    [],
    [],
)
subjects_train, subjects_val = [], []
domain_train, domain_val = [], []
for dataset_source in dataset_names:
    if dataset_source != dataset_target:
        X_, y_, subjects_ = data_dict[dataset_source]
        valid_size = 0.2
        (
            X_train_,
            X_val_,
            y_train_,
            y_val_,
            subjects_train_,
            subjects_val_,
        ) = train_test_split(X_, y_, subjects_, test_size=valid_size)

        X_train += X_train_
        X_val += X_val_
        y_train += y_train_
        y_val += y_val_
        subjects_train += subjects_train_
        subjects_val += subjects_val_
        domain_train += [dataset_source] * len(X_train_)
        domain_val += [dataset_source] * len(X_val_)
        print(f"Dataset {dataset_source}: {len(X_train_)}" f" train, {len(X_val_)} val")

if scaling == "subject":
    ma = MongeAlignment(n_jobs=n_jobs)
    X_train = ma.fit_transform(X_train)
    X_val = ma.transform(X_val)
    X_target = ma.transform(X_target)
    del ma

# %%

domains = np.concatenate(
    [[domain_train[i]] * len(X_train[i]) for i in range(len(domain_train))]
)
subjects = np.concatenate(
    [[subjects_train[i]] * len(X_train[i]) for i in range(len(subjects_train))]
)
dataset = SequenceDataset(
    np.concatenate(X_train, axis=0), np.concatenate(y_train, axis=0), subjects, domains
)
# %%
domains_val = np.concatenate(
    [[domain_val[i]] * len(X_val[i]) for i in range(len(domain_val))]
)
subjects_val_ = np.concatenate(
    [[subjects_val[i]] * len(X_val[i]) for i in range(len(subjects_val))]
)
dataset_val = SequenceDataset(
    np.concatenate(X_val, axis=0),
    np.concatenate(y_val, axis=0),
    subjects_val_,
    domains_val,
)

n_chans, n_time = X_train[0][0].shape
n_classes = len(np.unique(y_train[0]))
# %%

train_sampler = SequenceSampler(
    dataset.metadata, n_windows, n_windows_stride, random_state=seed, randomize=False
)

# %%

valid_sampler = SequenceSampler(
    dataset_val.metadata, n_windows, n_windows_stride, randomize=False
)

in_chans, input_size_samples = dataset[0][0].shape

# load state dict
model = torch.load("results/models/usleep_MASS.pt")
# %%

# %%

train_sampler = SequenceSampler(
    dataset.metadata, n_windows, n_windows_stride, random_state=seed, randomize=False
)
dataloader = torch.utils.data.DataLoader(
    dataset,
    batch_size=64,
    shuffle=False,
    num_workers=0,
    pin_memory=True,
    sampler=train_sampler,
)
# %%
encoder = model.encoder[0].block_prepool[:2].to("cpu")
tmanorm = TMANorm(filter_size=128)
tmanorm_2 = TMANorm(filter_size=128)

encoder.eval()
X_flatten_all = []
psd_input = []
psd_input_norm = []
psd_output_norm = []
psd_output = []
psd_output_2 = []
y_all = []
with torch.no_grad():
    for batch in dataloader:
        X, y = batch
        y_all.append(y)
        X_flatten = X.permute(0, 2, 1, 3)  # (B, C, S, T)
        X_flatten = X_flatten.flatten(start_dim=2)
        X_flatten_all.append(X_flatten)
        psd_input.append(welch_psd(X_flatten, window="hann", nperseg=128)[1])
        output = encoder(X_flatten)

        psd_output.append(welch_psd(output, window="hann", nperseg=128)[1])

        X_flatten_filtered = tmanorm(X_flatten)
        psd_input_norm.append(welch_psd(X_flatten_filtered, window="hann", nperseg=128)[1])
        output_filtered = encoder(X_flatten_filtered)
        psd_output_norm.append(welch_psd(output_filtered, window="hann", nperseg=128)[1])

        output_filtered_2 = tmanorm_2(output_filtered)

        psd_output_2.append(welch_psd(output_filtered_2, window="hann", nperseg=128)[1])



# %%
psd_input = torch.cat(psd_input, axis=0)
psd_input_norm = torch.cat(psd_input_norm, axis=0)
psd_output = torch.cat(psd_output, axis=0)
psd_output_norm = torch.cat(psd_output_norm, axis=0)
psd_output_2 = torch.cat(psd_output_2, axis=0)
# %%
y_all = torch.cat(y_all, axis=0)
# %%
fig, axes = plt.subplots(3, 2, figsize=(10, 10), sharex=True, )


axes[0, 0].plot(freqs, psd_input[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
axes[0, 0].set_xlim(0, 0.2)
axes[0, 0].set_title("PSD of one batch in the input space")
axes[0, 0].set_ylabel("Power")


axes[0, 1].plot(freqs, psd_output[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
axes[0, 1].set_xlim(0, 0.2)
axes[0, 1].set_title("PSD of one batch after one encoder")

axes[1, 0].plot(
    freqs, psd_input_norm[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue"
)
axes[1, 0].set_xlim(0, 0.2)
axes[1, 0].set_title("PSD of one batch in the input space after TMA")
axes[1, 0].set_xlabel("Frequency (Hz)")
axes[1, 0].set_ylabel("Power")

axes[1, 1].plot(
    freqs, psd_output_norm[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue"
)
axes[1, 1].set_xlim(0, 0.2)
axes[1, 1].set_title("PSD of one batch after TMA and one encoder")

axes[2, 1].plot(
    freqs, psd_output_2[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue"
)
axes[2, 1].set_xlim(0, 0.2)
axes[2, 1].set_title("PSD of one batch after TMA, one encoder and one other TMA")
axes[2, 1].set_xlabel("Frequency (Hz)")
axes[2, 1].set_ylabel("Power")
plt.tight_layout()

axes[2, 0].axis("off")

# %%
X_flatten = torch.cat(X_flatten_all, axis=0)
# %%
plt.plot(freqs, psd_input[200:211, 0].T.cpu().numpy(), alpha=0.7, color="cornflowerblue")
plt.xlim(0, 0.2)
plt.title("PSD of one batch in the input space")
plt.xlabel("Frequency (Hz)")
plt.ylabel("Power")

# %%
plt.plot(X_flatten[100, 0].cpu().numpy())
plt.xlim(0, 3000)

# %%
plt.plot(freqs, psd_input_norm[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
plt.xlim(0, 0.2)
plt.title("PSD of one batch in the input space after TMA")
plt.xlabel("Frequency (Hz)")
plt.ylabel("Power")

# %%
plt.plot(freqs, psd_output[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
plt.xlim(0, 0.2)
plt.title("PSD of one batch after one encoder")
plt.xlabel("Frequency (Hz)")
plt.ylabel("Power")

# %%
plt.plot(freqs, psd_output_norm[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
plt.xlim(0, 0.2)
plt.title("PSD of one batch after TMA and one encoder")
plt.xlabel("Frequency (Hz)")
plt.ylabel("Power")

# %%
plt.plot(freqs, psd_output_2[:, 0].T.cpu().numpy(), alpha=0.3, color="cornflowerblue")
plt.xlim(0, 0.2)
plt.title("PSD of one batch after TMA, one encoder and one other TMA")
plt.xlabel("Frequency (Hz)")
plt.ylabel("Power")
# %%
filter_size = 128
psd = welch_psd(X_flatten, window="hann", nperseg=filter_size)[1]
weights = torch.ones_like(psd) / psd.shape[-1]
new_barycenter = torch.sum(weights * torch.sqrt(psd), axis=0) ** 2
D = torch.sqrt(new_barycenter) / torch.sqrt(psd)
H = torch.fft.irfft(D)
H_shift = torch.fft.fftshift(H, dim=-1)

H_flip = torch.flip(H_shift, dims=[-1])
n_chan = X_flatten.shape[1]
n_batch = X_flatten.shape[0]
X_flatten_filtered = torch.cat(
    [
        torch.nn.functional.conv1d(
            X_flatten[i : i + 1],
            H_flip[i : i + 1].view(n_chan, 1, -1),
            padding="same",
            groups=n_chan,
        )
        for i in range(n_batch)
    ]
)

# %%
X_numpy = X_flatten.cpu().numpy()

# %%
# use psd of numpy
import scipy.signal as signal

psd_numpy = signal.welch(X_numpy, nperseg=filter_size, axis=-1, window="hann")[1]
barycenter_numpy = np.mean(np.sqrt(psd_numpy), axis=0) ** 2
D_numpy = np.sqrt(barycenter_numpy) / np.sqrt(psd_numpy)
H_numpy = np.fft.irfft(D_numpy)
H_shift_numpy = np.fft.fftshift(H_numpy, axes=-1)
X_filtered_numpy = np.zeros_like(X_numpy)
for i in range(X_numpy.shape[0]):
    for j in range(X_numpy.shape[1]):
        X_filtered_numpy[i, j] = signal.convolve(
            X_numpy[i, j], H_shift_numpy[i, j], mode="same"
        )
# %%
import matplotlib.pyplot as plt

plt.plot(psd_numpy[0, 0], label="numpy")
plt.plot(psd[0, 0].cpu().numpy(), label="torch")
plt.legend()
# plt.xlim(0, 20)

# %%
plt.plot(barycenter_numpy[0], label="numpy")
plt.plot(new_barycenter[0].cpu().numpy(), label="torch")
plt.legend()

# %%
plt.plot(D_numpy[0, 0], label="numpy")
plt.plot(D[0, 0].cpu().numpy(), label="torch")
plt.legend()
# %%
plt.plot(H_numpy[0, 0], label="numpy")
plt.plot(H[0, 0].cpu().numpy(), label="torch")
plt.legend()

# %%
plt.plot(H_shift_numpy[0, 0], label="numpy")
plt.plot(H_shift[0, 0].cpu().numpy(), label="torch")
plt.legend()
# %%
plt.plot(X_filtered_numpy[0, 0], label="numpy")
plt.plot(X_flatten_filtered[0, 0].cpu().numpy(), label="torch")
plt.legend()

# %%
plt.plot(X_numpy[0, 0], label="original")
plt.plot(X_filtered_numpy[0, 0], label="numpy")
plt.legend()

# %%
x_test = X_flatten[0, 0]
# %%
psd = torch.fft.rfft(x_test).abs() ** 2
# %%
plt.plot(fft.cpu().numpy())
# %%
x_test_numpy = x_test.cpu().numpy()
psd_numpy = np.abs(np.fft.rfft(x_test_numpy)) ** 2
# %%
plt.plot(psd_numpy)
plt.plot(psd.cpu().numpy())
# plt.xlim(0, 100)
# %%
nperseg = 256
noverlap = nperseg // 2
step = nperseg - noverlap
num_segments = (X_flatten.shape[-1] - noverlap) // step
segment = X_flatten[..., i * step : i * step + nperseg]

# %%
# Compute the FFT and PSD for the segment along the last axis
segment_fft = torch.fft.rfft(segment)
segment_psd = torch.abs(segment_fft) ** 2 / ( nperseg)
# %%
plt.plot(segment_psd[0, 0].cpu().numpy())
# %%
segment_numpy = segment.cpu().numpy()
# %%
fft_numpy= np.fft.rfft(segment_numpy)
# %%
psd = np.conjugate(fft_numpy) * fft_numpy
# %%
plt.plot(psd_numpy[0, 0])

# %%
psd_fft_numpy = np.abs(np.fft.rfft(X_numpy)) ** 2
# %%
psd_fft = torch.abs(torch.fft.rfft(X_flatten)) ** 2
# %%
plt.plot(psd_fft_numpy[0, 0])
plt.plot(psd_fft[0, 0].cpu().numpy())
plt.xlim(0, 100)
# %%
tmanorm = TMANorm(filter_size=128)

# %%
X_filtered = tmanorm(X_flatten)

# %%
plt.plot(X_flatten[0, 0].cpu().numpy())
plt.plot(X_filtered[0, 0].cpu().numpy())

# %%
i = 0
model = model.to("cpu")
pred_night = model(torch.tensor(X_train[i]).float().unsqueeze(0)).argmax(axis=1)[0]
plt.plot(pred_night.cpu().numpy(), label="pred per night", alpha=0.7)
plt.plot(y_train[i], label="ground truth", alpha=0.7)
plt.title("Prediction per night (acc {:.2f})".format(accuracy_score(y_train[i], pred_night)))
plt.legend()

# %%
train_sampler = SequenceSampler(
    dataset.metadata, n_windows, n_windows, random_state=seed, randomize=False
)
dataloader = torch.utils.data.DataLoader(
    dataset,
    batch_size=64,
    shuffle=False,
    num_workers=0,
    pin_memory=True,
    sampler=train_sampler,
)

# %%
y_preds = []
model.eval()
model = model.to("cpu")
with torch.no_grad():
    for batch in dataloader:
        X, y = batch
        y_pred = model(X).argmax(axis=1)
        y_preds.append(y_pred.flatten())
    y_pred = torch.cat(y_preds)
# %%
plt.plot(y_pred.cpu().numpy(), label="pred with sequence", alpha=0.7)
plt.plot(y_train[0], label="ground truth", alpha=0.7)
plt.title("Prediction with sequence (acc {:.2f})".format(accuracy_score(y_train[0][:len(y_pred)], y_pred)))
plt.legend()
# %%