import copy
import os
from os import path

import h5py
import nibabel as nib
import numpy as np
import pandas as pd
from bids_neuropoly import bids
from tqdm import tqdm
import re

from ivadomed import transforms as imed_transforms
# Here imed_loader refers temporarily to the loader_new.py, to fix when integrating in pipeline
from ivadomed.loader import utils as imed_loader_utils, loader_new as imed_loader, film as imed_film
# Here imed_obj_detect refers temporarily to the utils_new.py, to fix when integrating in pipeline
from ivadomed.object_detection import utils_new as imed_obj_detect
from ivadomed import utils as imed_utils


class Dataframe:
    """
    This class aims to create a dataset using an HDF5 file, which can be used by an adapative loader
    to perform curriculum learning, Active Learning or any other strategy that needs to load samples
    in a specific way.
    It works on RAM or on the fly and can be saved for later.

    Args:
        hdf5_file (hdf5): hdf5 file containing dataset information
        contrasts (list of str): List of the contrasts of interest.
        path (str): Dataframe path.
        target_suffix (list of str): List of suffix of targetted structures.
        roi_suffix (str): List of suffix of ROI masks.
        filter_slices (SliceFilter): Object that filters slices according to their content.
        dim (int): Choice 2 or 3, for 2D or 3D data respectively.

    Attributes:
        dim (int): Choice 2 or 3, for 2D or 3D data respectively.
        contrasts (list of str): List of the contrasts of interest.
        filter_slices (SliceFilter): Object that filters slices according to their content.
        df (pd.Dataframe): Dataframe containing dataset information
    """

    def __init__(self, hdf5_file, contrasts, path, target_suffix=None, roi_suffix=None,
                 filter_slices=False, dim=2):
        # Number of dimension
        self.dim = dim
        # List of all contrasts
        self.contrasts = copy.deepcopy(contrasts)

        if target_suffix:
            for gt in target_suffix:
                self.contrasts.append('gt/' + gt)
        else:
            self.contrasts.append('gt')

        if roi_suffix:
            for roi in roi_suffix:
                self.contrasts.append('roi/' + roi)
        else:
            self.contrasts.append('ROI')

        self.df = None
        self.filter = filter_slices

        # Data frame
        if os.path.exists(path):
            self.load_dataframe(path)
        else:
            self.create_df(hdf5_file)

    def shuffle(self):
        """Shuffle the whole data frame."""
        self.df = self.df.sample(frac=1)

    def load_dataframe(self, path):
        """Load the dataframe from a csv file.

        Args:
            path (str): Path to hdf5 file.
        """
        try:
            self.df = pd.read_csv(path)
            print("Dataframe has been correctly loaded from {}.".format(path))
        except FileNotFoundError:
            print("No csv file found")

    def save(self, path):
        """Save the dataframe into a csv file.

        Args:
            path (str): Path to hdf5 file.
        """
        try:
            self.df.to_csv(path, index=False)
            print("Dataframe has been saved at {}.".format(path))
        except FileNotFoundError:
            print("Wrong path.")

    def create_df(self, hdf5_file):
        """Generate the Data frame using the hdf5 file.

        Args:
            hdf5_file (hdf5): File containing dataset information
        """
        # Template of a line
        empty_line = {col: 'None' for col in self.contrasts}
        empty_line['Slices'] = 'None'

        # Initialize the data frame
        col_names = [col for col in empty_line.keys()]
        col_names.append('Subjects')
        df = pd.DataFrame(columns=col_names)
        print(hdf5_file.attrs['patients_id'])
        # Filling the data frame
        for subject in hdf5_file.attrs['patients_id']:
            # Getting the Group the corresponding patient
            grp = hdf5_file[subject]
            line = copy.deepcopy(empty_line)
            line['Subjects'] = subject

            # inputs
            assert 'inputs' in grp.keys()
            inputs = grp['inputs']
            for c in inputs.attrs['contrast']:
                if c in col_names:
                    line[c] = '{}/inputs/{}'.format(subject, c)
                else:
                    continue
            # GT
            assert 'gt' in grp.keys()
            inputs = grp['gt']
            for c in inputs.attrs['contrast']:
                key = 'gt/' + c
                for col in col_names:
                    if key in col:
                        line[col] = '{}/gt/{}'.format(subject, c)
                    else:
                        continue
            # ROI
            assert 'roi' in grp.keys()
            inputs = grp['roi']
            for c in inputs.attrs['contrast']:
                key = 'roi/' + c
                for col in col_names:
                    if key in col:
                        line[col] = '{}/roi/{}'.format(subject, c)
                    else:
                        continue

            # Adding slices & removing useless slices if loading in ram
            line['Slices'] = np.array(grp.attrs['slices'])

            # If the number of dimension is 2, we separate the slices
            if self.dim == 2:
                if self.filter:
                    for n in line['Slices']:
                        line_slice = copy.deepcopy(line)
                        line_slice['Slices'] = n
                        df = df.append(line_slice, ignore_index=True)

            else:
                df = df.append(line, ignore_index=True)

        self.df = df

    def clean(self, contrasts):
        """Aims to remove lines where one of the contrasts in not available.

        Agrs:
            contrasts (list of str): List of contrasts.
        """
        # Replacing 'None' values by np.nan
        self.df[contrasts] = self.df[contrasts].replace(to_replace='None', value=np.nan)
        # Dropping np.nan
        self.df = self.df.dropna()


class BIDStoHDF5:
    """Converts a BIDS dataset to a HDF5 file.

    Args:
        bids_df (BidsDataframe): Object containing dataframe with all BIDS image files and their metadata.
        path_data (list) or (str): Path(s) to BIDS datasets
        subject_lst (list): Subject filenames list.
        target_suffix (list): List of suffixes for target masks.
        roi_params (dict): Dictionary containing parameters related to ROI image processing.
        contrast_lst (list): List of the contrasts.
        path_hdf5 (str): Path and name of the hdf5 file.
        contrast_balance (dict): Dictionary controlling image contrasts balance.
        slice_axis (int): Indicates the axis used to extract slices: "axial": 2, "sagittal": 0, "coronal": 1.
        metadata_choice (str): Choice between "mri_params", "contrasts", None or False, related to FiLM.
        slice_filter_fn (SliceFilter): Class that filters slices according to their content.
        transform (Compose): Transformations.
        object_detection_params (dict): Object detection parameters.

    Attributes:
        dt (dtype): hdf5 special dtype.
        path_hdf5 (str): path to hdf5 file containing dataset information.
        filename_pairs (list): A list of tuples in the format (input filename list containing all modalities,ground \
            truth filename, ROI filename, metadata).
        metadata (dict): Dictionary containing metadata of input and gt.
        prepro_transforms (Compose): Transforms to be applied before training.
        transform (Compose): Transforms to be applied during training.
        has_bounding_box (bool): True if all metadata contains bounding box coordinates, else False.
        slice_axis (int): Indicates the axis used to extract slices: "axial": 2, "sagittal": 0, "coronal": 1.
        slice_filter_fn (SliceFilter): Object that filters slices according to their content.
    """

    def __init__(self, bids_df, path_data, subject_lst, target_suffix, contrast_lst, path_hdf5, contrast_balance=None,
                 slice_axis=2, metadata_choice=False, slice_filter_fn=None, roi_params=None, transform=None,
                 object_detection_params=None, soft_gt=False):
        print("Starting conversion")

        path_data = imed_utils.format_path_data(path_data)

        # Sort subject_lst and create a sub-dataframe from bids_df containing only subjects from subject_lst
        subject_lst = sorted(subject_lst)
        df_subjects = bids_df.df[bids_df.df['filename'].isin(subject_lst)]
        # Backward compatibility for subject_lst containing participant_ids instead of filenames
        if df_subjects.empty:
            df_subjects = bids_df.df[bids_df.df['participant_id'].isin(subject_lst)]
            subject_lst = sorted(df_subjects['filename'].to_list())

        self.soft_gt = soft_gt
        self.dt = h5py.special_dtype(vlen=str)
        # opening an hdf5 file with write access and writing metadata
        # self.hdf5_file = h5py.File(hdf5_name, "w")
        self.path_hdf5 = path_hdf5
        list_patients = []

        self.filename_pairs = []

        if metadata_choice == 'mri_params':
            self.metadata = {"FlipAngle": [], "RepetitionTime": [],
                             "EchoTime": [], "Manufacturer": []}

        self.prepro_transforms, self.transform = transform

        # Create a dictionary with the number of subjects for each contrast of contrast_balance
        tot = {contrast: df_subjects['suffix'].str.fullmatch(contrast).value_counts()[True]
               for contrast in contrast_balance.keys()}

        # Create a counter that helps to balance the contrasts
        c = {contrast: 0 for contrast in contrast_balance.keys()}


        # Get all subjects path from bids_df for bounding box
        get_all_subj_path = bids_df.df[bids_df.df['filename']
                                .str.contains('|'.join(bids_df.get_subject_fnames()))]['path'].to_list()

        # Load bounding box from list of path
        self.has_bounding_box = True
        bounding_box_dict = imed_obj_detect.load_bounding_boxes(object_detection_params,
                                                                get_all_subj_path,
                                                                slice_axis,
                                                                contrast_lst)

        # Get all derivatives filenames from bids_df
        all_deriv = bids_df.get_deriv_fnames()

        for subject in tqdm(subject_lst, desc="Loading dataset"):

            df_sub = df_subjects.loc[df_subjects['filename'] == subject]

            # Training & Validation: do not consider the contrasts over the threshold contained in contrast_balance
            contrast = df_sub['suffix'].values[0]
            if contrast in (contrast_balance.keys()):
                c[contrast] = c[contrast] + 1
                if c[contrast] / tot[contrast] > contrast_balance[contrast]:
                    continue

            target_filename, roi_filename = [None] * len(target_suffix), None

            derivatives = bids_df.df[bids_df.df['filename']
                          .str.contains('|'.join(bids_df.get_derivatives(subject, all_deriv)))]['path'].to_list()

            for deriv in derivatives:
                for idx, suffix in enumerate(target_suffix):
                    if suffix in deriv:
                        target_filename[idx] = deriv
                if not (roi_params["suffix"] is None) and roi_params["suffix"] in deriv:
                    roi_filename = [deriv]

            if (not any(target_filename)) or (not (roi_params["suffix"] is None) and (roi_filename is None)):
                continue

            metadata = df_sub.to_dict(orient='records')[0]
            metadata['contrast'] = contrast

            if len(bounding_box_dict):
                # Take only one bounding box for cropping
                metadata['bounding_box'] = bounding_box_dict[str(df_sub['path'].values[0])][0]

            if metadata_choice == 'mri_params':
                if not all([imed_film.check_isMRIparam(m, metadata, subject, self.metadata) for m in
                            self.metadata.keys()]):
                    continue

            # Get subj_id (prefix filename without modality suffix and extension)
            subj_id = re.sub(r'_' + df_sub['suffix'].values[0] + '.*', '', subject)

            self.filename_pairs.append((subj_id, [df_sub['path'].values[0]],
                                            target_filename, roi_filename, [metadata]))
            list_patients.append(subj_id)

        self.slice_axis = slice_axis
        self.slice_filter_fn = slice_filter_fn

        # Update HDF5 metadata
        with h5py.File(self.path_hdf5, "w") as hdf5_file:
            hdf5_file.attrs.create('patients_id', list(set(list_patients)), dtype=self.dt)
            hdf5_file.attrs['slice_axis'] = slice_axis

            hdf5_file.attrs['slice_filter_fn'] = [('filter_empty_input', True), ('filter_empty_mask', False)]
            hdf5_file.attrs['metadata_choice'] = metadata_choice

        # Save images into HDF5 file
        self._load_filenames()
        print("Files loaded.")

    def _load_filenames(self):
        """Load preprocessed pair data (input and gt) in handler."""
        with h5py.File(self.path_hdf5, "a") as hdf5_file:
            for subject_id, input_filename, gt_filename, roi_filename, metadata in self.filename_pairs:
                # Creating/ getting the subject group
                if str(subject_id) in hdf5_file.keys():
                    grp = hdf5_file[str(subject_id)]
                else:
                    grp = hdf5_file.create_group(str(subject_id))

                roi_pair = imed_loader.SegmentationPair(input_filename, roi_filename, metadata=metadata,
                                                        slice_axis=self.slice_axis, cache=False, soft_gt=self.soft_gt)

                seg_pair = imed_loader.SegmentationPair(input_filename, gt_filename, metadata=metadata,
                                                        slice_axis=self.slice_axis, cache=False, soft_gt=self.soft_gt)
                print("gt filename", gt_filename)
                input_data_shape, _ = seg_pair.get_pair_shapes()

                useful_slices = []
                input_volumes = []
                gt_volume = []
                roi_volume = []

                for idx_pair_slice in range(input_data_shape[-1]):

                    slice_seg_pair = seg_pair.get_pair_slice(idx_pair_slice)

                    self.has_bounding_box = imed_obj_detect.verify_metadata(slice_seg_pair, self.has_bounding_box)
                    if self.has_bounding_box:
                        imed_obj_detect.adjust_transforms(self.prepro_transforms, slice_seg_pair)

                    # keeping idx of slices with gt
                    if self.slice_filter_fn:
                        filter_fn_ret_seg = self.slice_filter_fn(slice_seg_pair)
                    if self.slice_filter_fn and filter_fn_ret_seg:
                        useful_slices.append(idx_pair_slice)

                    roi_pair_slice = roi_pair.get_pair_slice(idx_pair_slice)
                    slice_seg_pair, roi_pair_slice = imed_transforms.apply_preprocessing_transforms(self.prepro_transforms,
                                                                                                    slice_seg_pair,
                                                                                                    roi_pair_slice)

                    input_volumes.append(slice_seg_pair["input"][0])

                    # Handle unlabeled data
                    if not len(slice_seg_pair["gt"]):
                        gt_volume = []
                    else:
                        gt_volume.append((slice_seg_pair["gt"][0] * 255).astype(np.uint8) / 255.)

                    # Handle data with no ROI provided
                    if not len(roi_pair_slice["gt"]):
                        roi_volume = []
                    else:
                        roi_volume.append((roi_pair_slice["gt"][0] * 255).astype(np.uint8) / 255.)

                # Getting metadata using the one from the last slice
                input_metadata = slice_seg_pair['input_metadata'][0]
                gt_metadata = slice_seg_pair['gt_metadata'][0]
                roi_metadata = roi_pair_slice['input_metadata'][0]

                if grp.attrs.__contains__('slices'):
                    grp.attrs['slices'] = list(set(np.concatenate((grp.attrs['slices'], useful_slices))))
                else:
                    grp.attrs['slices'] = useful_slices

                # Creating datasets and metadata
                contrast = input_metadata['contrast']
                # Inputs
                print(len(input_volumes))
                print("grp= ", str(subject_id))
                key = "inputs/{}".format(contrast)
                print("key = ", key)
                if len(input_volumes) < 1:
                    print("list empty")
                    continue
                grp.create_dataset(key, data=input_volumes)
                # Sub-group metadata
                if grp['inputs'].attrs.__contains__('contrast'):
                    attr = grp['inputs'].attrs['contrast']
                    new_attr = [c for c in attr]
                    new_attr.append(contrast)
                    grp['inputs'].attrs.create('contrast', new_attr, dtype=self.dt)

                else:
                    grp['inputs'].attrs.create('contrast', [contrast], dtype=self.dt)

                # dataset metadata
                grp[key].attrs['input_filenames'] = input_metadata['input_filenames']
                grp[key].attrs['data_type'] = input_metadata['data_type']

                if "zooms" in input_metadata.keys():
                    grp[key].attrs["zooms"] = input_metadata['zooms']
                if "data_shape" in input_metadata.keys():
                    grp[key].attrs["data_shape"] = input_metadata['data_shape']
                if "bounding_box" in input_metadata.keys():
                    grp[key].attrs["bounding_box"] = input_metadata['bounding_box']

                # GT
                key = "gt/{}".format(contrast)
                grp.create_dataset(key, data=gt_volume)
                # Sub-group metadata
                if grp['gt'].attrs.__contains__('contrast'):
                    attr = grp['gt'].attrs['contrast']
                    new_attr = [c for c in attr]
                    new_attr.append(contrast)
                    grp['gt'].attrs.create('contrast', new_attr, dtype=self.dt)

                else:
                    grp['gt'].attrs.create('contrast', [contrast], dtype=self.dt)

                # dataset metadata
                grp[key].attrs['gt_filenames'] = input_metadata['gt_filenames']
                grp[key].attrs['data_type'] = gt_metadata['data_type']

                if "zooms" in gt_metadata.keys():
                    grp[key].attrs["zooms"] = gt_metadata['zooms']
                if "data_shape" in gt_metadata.keys():
                    grp[key].attrs["data_shape"] = gt_metadata['data_shape']
                if gt_metadata['bounding_box'] is not None:
                    grp[key].attrs["bounding_box"] = gt_metadata['bounding_box']

                # ROI
                key = "roi/{}".format(contrast)
                grp.create_dataset(key, data=roi_volume)
                # Sub-group metadata
                if grp['roi'].attrs.__contains__('contrast'):
                    attr = grp['roi'].attrs['contrast']
                    new_attr = [c for c in attr]
                    new_attr.append(contrast)
                    grp['roi'].attrs.create('contrast', new_attr, dtype=self.dt)

                else:
                    grp['roi'].attrs.create('contrast', [contrast], dtype=self.dt)

                # dataset metadata
                grp[key].attrs['roi_filename'] = roi_metadata['gt_filenames']
                grp[key].attrs['data_type'] = roi_metadata['data_type']

                if "zooms" in roi_metadata.keys():
                    grp[key].attrs["zooms"] = roi_metadata['zooms']
                if "data_shape" in roi_metadata.keys():
                    grp[key].attrs["data_shape"] = roi_metadata['data_shape']

                # Adding contrast to group metadata
                if grp.attrs.__contains__('contrast'):
                    attr = grp.attrs['contrast']
                    new_attr = [c for c in attr]
                    new_attr.append(contrast)
                    grp.attrs.create('contrast', new_attr, dtype=self.dt)

                else:
                    grp.attrs.create('contrast', [contrast], dtype=self.dt)


class HDF5Dataset:
    """HDF5 dataset object.

    Args:
        bids_df (BidsDataframe): Object containing dataframe with all BIDS image files and their metadata.
        path_data (list) or (str): Path(s) to BIDS datasets
        subject_lst (list of str): List of subject filenames.
        model_params (dict): Dictionary containing model parameters.
        target_suffix (list of str): List of suffixes of the target structures.
        contrast_params (dict): Dictionary containing contrast parameters.
        slice_axis (int): Indicates the axis used to extract slices: "axial": 2, "sagittal": 0, "coronal": 1.
        transform (Compose): Transformations.
        metadata_choice (str): Choice between "mri_params", "contrasts", None or False, related to FiLM.
        dim (int): Choice 2 or 3, for 2D or 3D data respectively.
        complet (bool): If True removes lines where contrasts is not available.
        slice_filter_fn (SliceFilter): Object that filters slices according to their content.
        roi_params (dict): Dictionary containing parameters related to ROI image processing.
        object_detection_params (dict): Object detection parameters.

    Attributes:
        cst_lst (list): Contrast list.
        gt_lst (list): Contrast label used for ground truth.
        roi_lst (list): Contrast label used for ROI cropping.
        dim (int): Choice 2 or 3, for 2D or 3D data respectively.
        filter_slices (SliceFilter): Object that filters slices according to their content.
        prepro_transforms (Compose): Transforms to be applied before training.
        transform (Compose): Transforms to be applied during training.
        df_object (pd.Dataframe): Dataframe containing dataset information.

    """

    def __init__(self, bids_df, path_data, subject_lst, model_params, target_suffix, contrast_params,
                 slice_axis=2, transform=None, metadata_choice=False, dim=2, complet=True,
                 slice_filter_fn=None, roi_params=None, object_detection_params=None, soft_gt=False):
        self.cst_lst = copy.deepcopy(contrast_params["contrast_lst"])
        self.gt_lst = copy.deepcopy(model_params["target_lst"] if "target_lst" in model_params else None)
        self.roi_lst = copy.deepcopy(model_params["roi_lst"] if "roi_lst" in model_params else None)
        self.dim = dim
        self.roi_params = roi_params if roi_params is not None else {"suffix": None, "slice_filter_roi": None}
        self.filter_slices = slice_filter_fn
        self.prepro_transforms, self.transform = transform

        metadata_choice = False if metadata_choice is None else metadata_choice
        # Getting HDS5 dataset file
        if not os.path.exists(model_params["path_hdf5"]):
            print("Computing hdf5 file of the data")
            bids_to_hdf5 = BIDStoHDF5(bids_df=bids_df,
                                      path_data=path_data,
                                      subject_lst=subject_lst,
                                      path_hdf5=model_params["path_hdf5"],
                                      target_suffix=target_suffix,
                                      roi_params=self.roi_params,
                                      contrast_lst=self.cst_lst,
                                      metadata_choice=metadata_choice,
                                      contrast_balance=contrast_params["balance"],
                                      slice_axis=slice_axis,
                                      slice_filter_fn=slice_filter_fn,
                                      transform=transform,
                                      object_detection_params=object_detection_params,
                                      soft_gt=soft_gt)

            self.path_hdf5 = bids_to_hdf5.path_hdf5
        else:
            self.path_hdf5 = model_params["path_hdf5"]

        # Loading dataframe object
        with h5py.File(self.path_hdf5, "r") as hdf5_file:
            self.df_object = Dataframe(hdf5_file, self.cst_lst, model_params["csv_path"],
                                       target_suffix=self.gt_lst, roi_suffix=self.roi_lst,
                                       dim=self.dim, filter_slices=slice_filter_fn)
            if complet:
                self.df_object.clean(self.cst_lst)
            print("after cleaning")
            print(self.df_object.df.head())

            self.initial_dataframe = self.df_object.df

            self.dataframe = copy.deepcopy(self.df_object.df)

            self.cst_matrix = np.ones([len(self.dataframe), len(self.cst_lst)], dtype=int)

            # RAM status
            self.status = {ct: False for ct in self.df_object.contrasts}

            ram = model_params["ram"] if "ram" in model_params else True
            if ram:
                self.load_into_ram(self.cst_lst)

    def load_into_ram(self, contrast_lst=None):
        """Aims to load into RAM the contrasts from the list.

        Args:
            contrast_lst (list of str): List of contrasts of interest.
        """
        keys = self.status.keys()
        with h5py.File(self.path_hdf5, "r") as hdf5_file:
            for ct in contrast_lst:
                if ct not in keys:
                    print("Key error: status has no key {}".format(ct))
                    continue
                if self.status[ct]:
                    print("Contrast {} already in RAM".format(ct))
                else:
                    print("Loading contrast {} in RAM...".format(ct), end='')
                    for sub in self.dataframe.index:
                        if self.filter_slices:
                            slices = self.dataframe.at[sub, 'Slices']
                            self.dataframe.at[sub, ct] = hdf5_file[self.dataframe.at[sub, ct]][np.array(slices)]
                    print("Done.")
                self.status[ct] = True

    def set_transform(self, transform):
        """Set the transforms."""
        self.transform = transform

    def __len__(self):
        """Get the dataset size, ie he number of subvolumes."""
        return len(self.dataframe)

    def __getitem__(self, index):
        """Get samples.

        Warning: For now, this method only supports one gt / roi.

        Args:
            index (int): Sample index.

        Returns:
            dict: Dictionary containing image and label tensors as well as metadata.
        """
        line = self.dataframe.iloc[index]
        # For HeMIS strategy. Otherwise the values of the matrix dont change anything.
        missing_modalities = self.cst_matrix[index]

        input_metadata = []
        input_tensors = []

        # Inputs
        with h5py.File(self.path_hdf5, "r") as f:
            for i, ct in enumerate(self.cst_lst):
                if self.status[ct]:
                    input_tensor = line[ct] * missing_modalities[i]
                else:
                    input_tensor = f[line[ct]][line['Slices']] * missing_modalities[i]

                input_tensors.append(input_tensor)
                # input Metadata
                metadata = imed_loader_utils.SampleMetadata({key: value for key, value in f['{}/inputs/{}'
                                                            .format(line['Subjects'], ct)].attrs.items()})
                metadata['slice_index'] = line["Slices"]
                metadata['missing_mod'] = missing_modalities
                metadata['crop_params'] = {}
                input_metadata.append(metadata)

            # GT
            gt_img = []
            gt_metadata = []
            for idx, gt in enumerate(self.gt_lst):
                if self.status['gt/' + gt]:
                    gt_data = line['gt/' + gt]
                else:
                    gt_data = f[line['gt/' + gt]][line['Slices']]

                gt_data = gt_data.astype(np.uint8)
                gt_img.append(gt_data)
                gt_metadata.append(imed_loader_utils.SampleMetadata({key: value for key, value in
                                                                     f[line['gt/' + gt]].attrs.items()}))
                gt_metadata[idx]['crop_params'] = {}

            # ROI
            roi_img = []
            roi_metadata = []
            if self.roi_lst:
                if self.status['roi/' + self.roi_lst[0]]:
                    roi_data = line['roi/' + self.roi_lst[0]]
                else:
                    roi_data = f[line['roi/' + self.roi_lst[0]]][line['Slices']]

                roi_data = roi_data.astype(np.uint8)
                roi_img.append(roi_data)

                roi_metadata.append(imed_loader_utils.SampleMetadata({key: value for key, value in
                                                                      f[
                                                                          line['roi/' + self.roi_lst[0]]].attrs.items()}))
                roi_metadata[0]['crop_params'] = {}

            # Run transforms on ROI
            # ROI goes first because params of ROICrop are needed for the followings
            stack_roi, metadata_roi = self.transform(sample=roi_img,
                                                     metadata=roi_metadata,
                                                     data_type="roi")
            # Update metadata_input with metadata_roi
            metadata_input = imed_loader_utils.update_metadata(metadata_roi, input_metadata)

            # Run transforms on images
            stack_input, metadata_input = self.transform(sample=input_tensors,
                                                         metadata=metadata_input,
                                                         data_type="im")
            # Update metadata_input with metadata_roi
            metadata_gt = imed_loader_utils.update_metadata(metadata_input, gt_metadata)

            # Run transforms on images
            stack_gt, metadata_gt = self.transform(sample=gt_img,
                                                   metadata=metadata_gt,
                                                   data_type="gt")
            data_dict = {
                'input': stack_input,
                'gt': stack_gt,
                'roi': stack_roi,
                'input_metadata': metadata_input,
                'gt_metadata': metadata_gt,
                'roi_metadata': metadata_roi
            }

            return data_dict

    def update(self, strategy="Missing", p=0.0001):
        """Update the Dataframe itself.

        Args:
            p (float): Float between 0 and 1, probability of the contrast to be missing.
            strategy (str): Update the dataframe using the corresponding strategy. For now the only the strategy
                implemented is the one used by HeMIS (i.e. by removing contrasts with a certain probability.) Other
                strategies that could be implemented are Active Learning, Curriculum Learning, ...
        """
        if strategy == 'Missing':
            print("Probalility of missing contrast = {}".format(p))
            for idx in range(len(self.dataframe)):
                missing_mod = np.random.choice(2, len(self.cst_lst), p=[p, 1 - p])
                # if all contrasts are removed from a subject randomly choose 1
                if not np.any(missing_mod):
                    missing_mod = np.zeros((len(self.cst_lst)))
                    missing_mod[np.random.randint(2, size=1)] = 1
                self.cst_matrix[idx, ] = missing_mod

            print("Missing contrasts = {}".format(self.cst_matrix.size - self.cst_matrix.sum()))


def HDF5ToBIDS(path_hdf5, subjects, path_dir):
    """Convert HDF5 file to BIDS dataset.

    Args:
        path_hdf5 (str): Path to the HDF5 file.
        subjects (list): List of subject names.
        path_dir (str): Output folder path, already existing.
    """
    # Open FDH5 file
    with h5py.File(path_hdf5, "r") as hdf5_file:
        # check the dir exists:
        if not path.exists(path_dir):
            raise FileNotFoundError("Directory {} doesn't exist. Stopping process.".format(path_dir))

        # loop over all subjects
        for sub in subjects:
            path_sub = path_dir + '/' + sub + '/anat/'
            path_label = path_dir + '/derivatives/labels/' + sub + '/anat/'

            if not path.exists(path_sub):
                os.makedirs(path_sub)

            if not path.exists(path_label):
                os.makedirs(path_label)

            # Get Subject Group
            try:
                grp = hdf5_file[sub]
            except Exception:
                continue
            # inputs
            cts = grp['inputs'].attrs['contrast']

            # Relation between voxel and world coordinates is not available
            for ct in cts:
                input_data = np.array(grp['inputs/{}'.format(ct)])
                nib_image = nib.Nifti1Image(input_data, np.eye(4))
                filename = os.path.join(path_sub, sub + "_" + ct + ".nii.gz")
                nib.save(nib_image, filename)

            # GT
            cts = grp['gt'].attrs['contrast']

            for ct in cts:
                for filename in grp['gt/{}'.format(ct)].attrs['gt_filenames']:
                    gt_data = grp['gt/{}'.format(ct)]
                    nib_image = nib.Nifti1Image(gt_data, np.eye(4))
                    filename = os.path.join(path_label, filename.split("/")[-1])
                    nib.save(nib_image, filename)

            cts = grp['roi'].attrs['contrast']

            for ct in cts:
                roi_data = grp['roi/{}'.format(ct)]
                if np.any(roi_data.shape):
                    nib_image = nib.Nifti1Image(roi_data, np.eye(4))
                    filename = os.path.join(path_label,
                                            grp['roi/{}'.format(ct)].attrs['roi_filename'][0].split("/")[-1])
                    nib.save(nib_image, filename)