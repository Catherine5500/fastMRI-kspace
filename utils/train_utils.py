import torch
from torch import nn, optim
from torch.utils.data import DataLoader
import torch.nn.functional as F

from pathlib import Path

from data.mri_data import SliceData
from data.data_transforms import complex_abs, ifft2


class CheckpointManager:
    """
    A checkpoint manager for Pytorch models and optimizers loosely based on Keras/Tensorflow Checkpointers.
    I should note that I am not sure whether this works in Pytorch graph mode.
    Giving up on saving as HDF5 files like in Keras. Just too annoying.
    Note that the whole system is based on 1 indexing, not 0 indexing.
    """
    def __init__(self, model, optimizer, mode='min', save_best_only=True, ckpt_dir='./checkpoints', max_to_keep=5):

        # Type checking.
        assert isinstance(model, nn.Module), 'Not a Pytorch Model'
        assert isinstance(optimizer, optim.Optimizer), 'Not a Pytorch Optimizer'
        assert isinstance(max_to_keep, int) and (max_to_keep >= 0), 'Not a non-negative integer'
        assert mode in ('min', 'max'), 'Mode must be either `min` or `max`'
        ckpt_path = Path(ckpt_dir)
        assert ckpt_path.exists(), 'Not a valid, existing path'

        record_path = ckpt_path / 'Checkpoints.txt'

        try:
            record_file = open(record_path, mode='x')
        except FileExistsError:
            import sys
            print('WARNING: It is recommended to have a separate checkpoint directory for each run.', file=sys.stderr)
            print('Appending to previous Checkpoint record file!', file=sys.stderr)
            record_file = open(record_path, mode='a')

        print(f'Checkpoint List for {ckpt_path}', file=record_file)
        record_file.close()

        self.model = model
        self.optimizer = optimizer
        self.save_best_only = save_best_only
        self.ckpt_path = ckpt_path
        self.max_to_keep = max_to_keep
        self.save_counter = 0
        self.record_path = record_path
        self.record_dict = dict()

        if mode == 'min':
            self.prev_best = float('inf')
            self.mode = mode
        elif mode == 'max':
            self.prev_best = -float('inf')
            self.mode = mode
        else:
            raise TypeError('Mode must be either `min` or `max`')

    def _save(self, ckpt_name=None, **save_kwargs):
        self.save_counter += 1
        save_dict = {'model_state_dict': self.model.state_dict(), 'optimizer_state_dict': self.optimizer.state_dict()}
        save_dict.update(save_kwargs)
        save_path = self.ckpt_path / (f'{ckpt_name}.tar' if ckpt_name else f'ckpt_{self.save_counter:03d}.tar')

        torch.save(save_dict, save_path)
        print(f'Saved Checkpoint to {save_path}')
        print(f'Checkpoint {self.save_counter:04d}: {save_path}')

        with open(file=self.record_path, mode='a') as file:
            print(f'Checkpoint {self.save_counter:04d}: {save_path}', file=file)

        self.record_dict[self.save_counter] = save_path

        if self.save_counter > self.max_to_keep:
            for count, ckpt_path in self.record_dict.items():  # This system uses 1 indexing.
                if (count <= (self.save_counter - self.max_to_keep)) and ckpt_path.exists():
                    ckpt_path.unlink()  # Delete existing checkpoint

        return save_path

    def save(self, metric, verbose=True, ckpt_name=None, **save_kwargs):  # save_kwargs are extra variables to save
        if self.mode == 'min':
            is_best = metric < self.prev_best
        elif self.mode == 'max':
            is_best = metric > self.prev_best
        else:
            raise TypeError('Mode must be either `min` or `max`')

        save_path = None
        if is_best or not self.save_best_only:
            save_path = self._save(ckpt_name, **save_kwargs)

        if verbose:
            if is_best:
                print(f'Metric improved from {self.prev_best:.4e} to {metric:.4e}')
            else:
                print(f'Metric did not improve.')

        if is_best:  # Update new best metric.
            self.prev_best = metric

        # Returns where the file was saved if any was saved. Also returns whether this was the best on the metric.
        return save_path, is_best  # So that one can see whether this one is the best or not.

    def load(self, load_dir, load_optimizer=True):
        save_dict = torch.load(load_dir)

        self.model.load_state_dict(save_dict['model_state_dict'])
        print(f'Loaded model parameters from {load_dir}')

        if load_optimizer:
            self.optimizer.load_state_dict(save_dict['optimizer_state_dict'])
            print(f'Loaded optimizer parameters from {load_dir}')

    def load_latest(self, load_root):
        load_root = Path(load_root)
        load_dir = sorted([x for x in load_root.iterdir() if x.is_dir()])[-1]
        load_file = sorted([x for x in load_dir.iterdir() if x.is_file()])[-1]

        print('Loading', load_file)
        self.load(load_file, load_optimizer=False)
        print('Done')


def load_model_from_checkpoint(model, load_dir):
    """
    A simple function for loading checkpoints without having to use Checkpoint Manager. Very useful for evaluation.
    Checkpoint manager was designed for loading checkpoints before resuming training.

    model (nn.Module): Model architecture to be used.
    load_dir (str): File path to the checkpoint file. Can also be a Path instead of a string.
    """
    assert isinstance(model, nn.Module), 'Model must be a Pytorch module.'
    assert Path(load_dir).exists(), 'The specified directory does not exist'
    save_dict = torch.load(load_dir)
    model.load_state_dict(save_dict['model_state_dict'])
    return model  # Not actually necessary to return the model but doing so anyway.


def create_datasets(args, train_transform, val_transform):
    assert callable(train_transform) and callable(val_transform), 'Transforms should be callable functions.'

    # Generating Datasets.
    train_dataset = SliceData(
        root=Path(args.data_root) / f'{args.challenge}_train',
        transform=train_transform,
        challenge=args.challenge,
        sample_rate=args.sample_rate,
        use_gt=False
    )

    val_dataset = SliceData(
        root=Path(args.data_root) / f'{args.challenge}_val',
        transform=val_transform,
        challenge=args.challenge,
        sample_rate=args.sample_rate,
        use_gt=False
    )
    return train_dataset, val_dataset


def single_collate_fn(batch):  # Returns `targets` as a 4D Tensor.
    """
    hack for single batch case.
    """
    temp = batch[0]
    return temp[0].unsqueeze(0), temp[1].unsqueeze(0), temp[2]


def multi_collate_fn(batch):
    tensors = list()
    targets = list()
    scales = list()

    with torch.no_grad():
        for (tensor, target, scaling) in batch:
            tensors.append(tensor)
            targets.append(target)  # Note that targets are 3D Tensors in a list, not 4D.
            scales.append(scaling)

        max_width = max(tensor.size(-1) for tensor in tensors)

        # Assumes that padding for UNET divisor has already been performed for each slice.
        for idx in range(len(tensors)):
            pad = (max_width - tensors[idx].size(-1)) // 2
            tensors[idx] = F.pad(tensors[idx], pad=[pad, pad], value=0)

    return torch.stack(tensors, dim=0), targets, scales


def create_data_loaders(args, train_transform, val_transform):

    """
    A function for creating datasets where the data is sent to the desired device before being given to the model.
    This is done because data transfer is a serious bottleneck in k-space learning and is best done asynchronously.
    Also, the Fourier Transform is best done on the GPU instead of on CPU.
    Finally, Sending k-space data to device beforehand removes the need to also send generated label data to device.
    This reduces data transfer significantly.
    The only problem is that sending to GPU cannot be batched with this method.
    However, this seems to be a small price to pay.
    """
    assert callable(train_transform) and callable(val_transform), 'Transforms should be callable functions.'

    train_dataset, val_dataset = create_datasets(args, train_transform, val_transform)

    if args.batch_size == 1:
        collate_fn = single_collate_fn
    elif args.batch_size > 1:
        collate_fn = multi_collate_fn
    else:
        raise RuntimeError('Invalid batch size')

    # Generating Data Loaders
    train_loader = DataLoader(
        dataset=train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        pin_memory=args.pin_memory,
        collate_fn=collate_fn
    )

    val_loader = DataLoader(
        dataset=val_dataset,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_memory,
        collate_fn=collate_fn
    )
    return train_loader, val_loader


def make_grid_triplet(image_recons, targets):

    # Simple hack. Just use the first element if the input is a list for batching implementation.
    if isinstance(image_recons, list) and isinstance(targets, list):
        # Recall that in the mini-batched implementation, the outputs are lists of 3D Tensors.
        image_recons = image_recons[0].unsqueeze(dim=0)
        targets = targets[0].unsqueeze(dim=0)

    if image_recons.size(0) > 1:
        raise NotImplementedError('Mini-batch size greater than 1 has not been implemented yet.')

    assert image_recons.size() == targets.size()

    large = torch.max(targets)
    small = torch.min(targets)
    diff = large - small

    # Scaling to 0~1 range.
    image_recons = (image_recons.clamp(min=small, max=large) - small) / diff
    targets = (targets - small) / diff

    # Send to CPU if necessary. Assumes batch size of 1.
    image_recons = image_recons.detach().cpu().squeeze(dim=0)
    targets = targets.detach().cpu().squeeze(dim=0)

    if image_recons.size(0) == 15:
        image_recons = torch.cat(torch.chunk(image_recons.view(-1, image_recons.size(-1)), chunks=5, dim=0), dim=1)
        targets = torch.cat(torch.chunk(targets.view(-1, targets.size(-1)), chunks=5, dim=0), dim=1)
    elif image_recons.size(0) == 1:
        image_recons = image_recons.squeeze()
        targets = targets.squeeze()
    else:
        raise ValueError('Invalid dimensions!')

    deltas = targets - image_recons

    return image_recons, targets, deltas


def make_k_grid(kspace_recons, smoothing_factor=4):
    """
    Function for making k-space visualizations for Tensorboard.
    """
    # Simple hack. Just use the first element if the input is a list --> batching implementation.
    if isinstance(kspace_recons, list):
        kspace_recons = kspace_recons[0].unsqueeze(dim=0)

    if kspace_recons.size(0) > 1:
        raise NotImplementedError('Mini-batch size greater than 1 has not been implemented yet.')

    # Assumes that the smallest values will be close enough to 0 as to not matter much.
    kspace_view = complex_abs(kspace_recons.detach()).squeeze(dim=0)
    # Scaling & smoothing.
    # smoothing_factor converted to float32 tensor. expm1 and log1p require float32 tensors.
    # They cannot accept python integers.
    sf = torch.as_tensor(smoothing_factor, dtype=torch.float32)
    kspace_view *= torch.expm1(sf) / kspace_view.max()
    kspace_view = torch.log1p(kspace_view)  # Adds 1 to input for natural log.
    kspace_view /= kspace_view.max()  # Normalization to 0~1 range.
    kspace_view = kspace_view.cpu()

    if kspace_view.size(0) == 15:
        kspace_view = torch.cat(torch.chunk(kspace_view.view(-1, kspace_view.size(-1)), chunks=5, dim=0), dim=1)

    return kspace_view.squeeze()


def visualize_from_kspace(kspace_recons, kspace_targets, smoothing_factor=4):
    """
    Assumes that all values are on the same scale and have the same shape.
    """
    image_recons = complex_abs(ifft2(kspace_recons))
    image_targets = complex_abs(ifft2(kspace_targets))
    image_recons, image_targets, image_deltas = make_grid_triplet(image_recons, image_targets)
    kspace_targets = make_k_grid(kspace_targets, smoothing_factor)
    kspace_recons = make_k_grid(kspace_recons, smoothing_factor)
    return kspace_recons, kspace_targets, image_recons, image_targets, image_deltas





