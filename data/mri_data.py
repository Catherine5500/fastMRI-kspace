import pathlib
import random
from math import ceil

import h5py
from torch.utils.data import Dataset


class SliceData(Dataset):
    """
    A PyTorch Dataset that provides access to MR image slices.
    """

    def __init__(self, root, transform, challenge, sample_rate=1, use_gt=True):
        """
        Args:
            root (pathlib.Path): Path to the dataset.
            transform (callable): A callable object that pre-processes the raw data into
                appropriate form. The transform function should take 'kspace', 'target',
                'attributes', 'filename', and 'slice_num' as inputs. 'target' may be null
                for test data.
            challenge (str): "singlecoil" or "multicoil" depending on which challenge to use.
            sample_rate (float, optional): A float between 0 and 1. This controls what fraction
                of the volumes should be loaded.
            use_gt (bool): Whether to load the ground truth 320x320 fully-sampled reconstructions or not.
                Very useful for reducing data I/O in k-space learning.
        """
        self.use_gt = use_gt
        if challenge not in ('singlecoil', 'multicoil'):
            raise ValueError('challenge should be either "singlecoil" or "multicoil"')

        self.transform = transform
        self.recons_key = 'reconstruction_esc' if challenge == 'singlecoil' else 'reconstruction_rss'

        self.examples = list()
        files = list(pathlib.Path(root).glob('*.h5'))

        if not files:  # If the list is empty for any reason
            raise OSError("Sorry! No files present in this directory. "
                          "Please check if your disk has been loaded.")

        print(f'Initializing {root}. This might take a minute.')

        if sample_rate < 1:
            random.shuffle(files)
            num_files = ceil(len(files) * sample_rate)
            files = files[:num_files]

        for file_name in sorted(files):
            kspace = h5py.File(file_name, mode='r')['kspace']
            num_slices = kspace.shape[0]
            self.examples += [(file_name, slice_num) for slice_num in range(num_slices)]

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        file_path, slice_num = self.examples[idx]
        with h5py.File(file_path, mode='r') as data:
            kspace = data['kspace'][slice_num]
            if self.recons_key in data and self.use_gt:
                target = data[self.recons_key][slice_num]
            else:
                target = None
            return self.transform(kspace, target, data.attrs, file_path.name, slice_num)
