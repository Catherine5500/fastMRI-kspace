import torch
import torch.nn.functional as F

import numpy as np

from data.data_transforms import to_tensor, ifft2, complex_abs, apply_mask, k_slice_to_nchw


# My transforms for data processing
class TrainSliceTransform:
    """
    Data Transformer for training and validating models.

    Note that this method only works well for mini-batch of 1.
    Using a larger mini-batch would require processing at the batch level.
    """

    def __init__(self, mask_func, which_challenge, use_seed=True, divisor=1):
        """
        Args:
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.
            use_seed (bool): If true, this class computes a pseudo random number generator seed
                from the filename. This ensures that the same mask is used for all the slices of
                a given volume every time.
            divisor (int): An integer indicating the lowest common denominator necessary for padding.
                This parameter is necessary because phase encoding dimensions are different for all blocks
                and UNETs and other models require inputs to be divisible by some power of 2.
                Set to 1 if not necessary.
        """

        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.mask_func = mask_func
        self.which_challenge = which_challenge
        self.use_seed = use_seed
        self.divisor = divisor

    def __call__(self, kspace, target, attrs, file_name, slice_num):
        """
        Args:
            kspace (numpy.array): Input k-space of shape (num_coils, height, width) for multi-coil
                data or (rows, cols) for single coil data.
            target (numpy.array): Target (320x320) image. May be None.
            attrs (dict): Acquisition related information stored in the HDF5 object.
            file_name (str): File name
            slice_num (int): Serial number of the slice.
        Returns:
            (tuple): tuple containing:
                data (torch.Tensor): kspace data converted to CHW format for CNNs, where C=(2*num_coils).
                    Also has padding in the width axis for auto-encoders, which have down-sampling regions.
                    This requires the data to be divisible by some number (usually 2**num_pooling_layers).
                    Otherwise, concatenation will not work in the decoder due to different sizes.
                    Only the width dimension is padded in this case due to the nature of the dataset.
                    The height is fixed at 640, while the width is variable.
                labels (torch.Tensor): Coil-wise ground truth images. Shape=(num_coils, H, W)
        """
        assert np.iscomplexobj(kspace), 'kspace must be complex.'
        assert kspace.shape[-1] % 2 == 0, 'k-space data width must be even.'

        if kspace.ndim == 2:  # For singlecoil. Makes data processing later on much easier.
            kspace = np.expand_dims(kspace, axis=0)
        elif kspace.ndim != 3:  # Prevents possible errors.
            raise TypeError('Invalid slice type')

        with torch.no_grad():  # Remove unnecessary gradient calculations.

            kspace = to_tensor(kspace)  # Now a Tensor of (num_coils, height, width, 2), where 2 is (real, imag).
            labels = complex_abs(ifft2(kspace))
            # Apply mask
            seed = None if not self.use_seed else tuple(map(ord, file_name))
            masked_kspace, mask = apply_mask(kspace, self.mask_func, seed)

            data = k_slice_to_nchw(masked_kspace)
            # divisor = 2 ** 4  # Because there are 4 pooling layers. Change later for generalizability.
            pad = (self.divisor - (data.shape[-1] % self.divisor)) // 2
            pad = [pad, pad]
            data = F.pad(data, pad=pad, value=0)  # This pads at the last dimension of a tensor.

            # Using the data acquisition method (fat suppression) may be useful later on.

        return data, labels


class SubmitSliceTransform:
    """
    Data Transformer for generating submissions on the validation and test datasets.
    """

    def __init__(self, which_challenge, mask_func=None, divisor=1):
        """
        Args:
            which_challenge (str): Either "singlecoil" or "multicoil" denoting the dataset.
            mask_func (MaskFunc): A function that can create a mask of appropriate shape.
        """
        if which_challenge not in ('singlecoil', 'multicoil'):
            raise ValueError(f'Challenge should either be "singlecoil" or "multicoil"')
        self.which_challenge = which_challenge
        self.mask_func = mask_func
        self.divisor = divisor

    def __call__(self, kspace, target, attrs, file_name, slice_num):
        """
        Args:
            kspace (numpy.Array): k-space measurements
            target (numpy.Array): Target image
            attrs (dict): Acquisition related information stored in the HDF5 object
            file_name (str): File name
            slice_num (int): Serial number of the slice
        Returns:
            (tuple): tuple containing:
                image (torch.Tensor): Normalized zero-filled input image
                mean (float): Mean of the zero-filled image
                std (float): Standard deviation of the zero-filled image
                file_name (str): File name
                slice_num (int): Serial number of the slice
        """
        kspace = to_tensor(kspace)
        if self.mask_func is not None:  # Validation set
            seed = tuple(map(ord, file_name))
            masked_kspace, _ = apply_mask(kspace, self.mask_func, seed)
        else:  # Test set
            masked_kspace = kspace

        data = k_slice_to_nchw(masked_kspace)
        pad = (self.divisor - (data.shape[-1] % self.divisor)) // 2
        pad = [pad, pad]
        data = F.pad(data, pad=pad, value=0)  # This pads at the last dimension of a tensor.
        return data
