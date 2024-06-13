# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Pipeline for generating tensorflow examples from satellite images."""

import binascii
import collections
import csv
import dataclasses
import hashlib
import itertools
import json
import logging
import os
import pickle
import struct
import typing
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import apache_beam as beam
import cv2
import geopandas as gpd
import numpy as np
from openlocationcode import openlocationcode
import shapely.geometry
import shapely.wkb
from skai import beam_utils
from skai import buildings
from skai import cloud_detector
from skai import earth_engine
from skai import open_street_map
from skai import read_raster
from skai import utils
import tensorflow as tf


Example = tf.train.Example
Metrics = beam.metrics.Metrics
Polygon = shapely.geometry.polygon.Polygon

# If more than this fraction of a before or after image is blank, discard this
# example.
_BLANK_THRESHOLD = 0.25

# Technique used for aligning before and after images. See the OpenCV
# documentation on template matching for the list of options.
_ALIGNMENT_METHOD = cv2.TM_CCOEFF_NORMED

# Maximum number of pixels that an image can be displaced during alignment.
_MAX_DISPLACEMENT = 30

# Code length generated by openlocationcode module.
_PLUS_CODE_LENGTH = 14


@dataclasses.dataclass
class ExamplesGenerationConfig:
  """A configuration for generate_examples_main."""

  dataset_name: str
  output_dir: str
  before_image_patterns: Optional[List[str]] = dataclasses.field(
      default_factory=list
  )
  after_image_patterns: Optional[List[str]] = dataclasses.field(
      default_factory=list
  )
  aoi_path: Optional[str] = None
  before_image_config: Optional[str] = None
  after_image_config: Optional[str] = None
  cloud_project: Optional[str] = None
  cloud_region: Optional[str] = None
  use_dataflow: bool = False
  output_metadata_file: bool = True
  worker_service_account: Optional[str] = None
  max_dataflow_workers: int = 20
  example_patch_size: int = 64
  large_patch_size: int = 256
  resolution: float = 0.5
  output_shards: int = 20
  gdal_env: List[str] = dataclasses.field(default_factory=list)
  buildings_method: str = 'file'  # file, open_street_map, open_buildings, none
  buildings_file: Optional[str] = None
  overpass_url: Optional[str] = 'https://lz4.overpass-api.de/api/interpreter'
  open_buildings_feature_collection: Optional[str] = (
      'GOOGLE/Research/open-buildings/v3/polygons'
  )
  open_buildings_confidence: float = 0.0
  earth_engine_service_account: Optional[str] = ''
  earth_engine_private_key: Optional[str] = None
  labels_file: Optional[str] = None
  label_property: Optional[str] = None
  labels_to_classes: Optional[List[str]] = None
  num_keep_labeled_examples: int = None
  configuration_path: Optional[str] = None
  cloud_detector_model_path: Optional[str] = None

  # TODO(mohammedelfatihsalah): Add a type for flagvalues argument in init_from_flags.
  @staticmethod
  def init_from_flags(flagvalues):
    """Intialize configuration from command flags.

    Args:
      flagvalues: The flage values for configuration values.

    Returns:
      An ExampleGenerationConfig.
    Raises:
      AttributeError: if dataset_name or output_dir doesnot exist in the
      flagvalues.
    """
    dataset_name = flagvalues.__getattr__('dataset_name')
    output_dir = flagvalues.__getattr__('output_dir')
    config = ExamplesGenerationConfig(
        dataset_name=dataset_name, output_dir=output_dir
    )
    for field in dataclasses.fields(ExamplesGenerationConfig):
      try:
        val = flagvalues.__getattr__(field.name)
        if val:
          config.__setattr__(field.name, val)
      except AttributeError:
        logging.info(
            (
                '%s is not found so a default value will be used for it with a'
                ' a value %f'
            ),
            field.name,
            getattr(config, field.name),
        )
    return config

  @staticmethod
  def init_from_json_path(json_path: str):
    """Intialize configuration from json file.

    Args:
      json_path: the path to the json file that contain configuration values.

    Returns:
     An ExampleGenerationConfig.
    Raises:
      KeyError if dataset_name or output_dir are not in the json file.
    """
    with tf.io.gfile.GFile(json_path) as f:
      data = json.load(f)
      output_dir = data['output_dir']
      dataset_name = data['dataset_name']
      config = ExamplesGenerationConfig(
          dataset_name=dataset_name, output_dir=output_dir
      )
      for field in dataclasses.fields(ExamplesGenerationConfig):
        try:
          val = data[field.name]
          config.__setattr__(field.name, val)
        except KeyError:
          logging.info(
              (
                  '%s is not given in the json config file so a default value'
                  ' will be it %s will be used.'
              ),
              field.name,
              getattr(config, field.name)
          )
    return config


@dataclasses.dataclass
class _FeatureUnion:
  """Class that holds all possible feature types for an example.

  Objects of this class should have exactly one non-null attribute. Currently
  it can either be a dictionary of scalar features, or before or after images.

  Attributes:
    scalar_features: Dictionary mapping string feature names to lists of scalar
        values (floats, ints, or strings).
    before_image: Before image. Should be a tuple of (image_path, image array).
    after_image: After image. Should be a tuple of (image_path, image array).
  """
  scalar_features: Dict[str, Any] = None
  before_image: Tuple[str, np.ndarray] = None
  after_image: Tuple[str, np.ndarray] = None


class NoBuildingFoundError(Exception):
  """Raised when no building found in the area of interest."""

  def __init__(self):
    super().__init__('No building found.')


class NotInitializedEarthEngineError(Exception):
  """Raised when earth engine couldnot be initialized."""

  def __init__(self):
    super().__init__('Earth Engine could not be initialized.')


def download_building_footprints(
    config, regions: list[Polygon], output_path: str
) -> None:
  """Finds building centroids based on flag settings.

  This function is meant to be called from generate_examples_main.py.

  Args:
    config: A configuration object that specify how to get the building
      centroids.
    regions: List of polygons of regions to find buildings in.
    output_path: Path to write buildings file to.

  Raises:
    ValueError: if buildings_method flag has unknown value.
    NotInitializedEarthEngineError: if earth couldnot be initialized.
    NoBuildingFoundError: if no building is found in the area of interest.
  """
  if config.buildings_method == 'file':
    buildings.convert_buildings_file(
        config.buildings_file, regions, output_path
    )
  elif config.buildings_method == 'open_street_map':
    open_street_map.get_building_centroids_in_regions(
        regions, config.overpass_url, output_path
    )
  elif config.buildings_method == 'open_buildings':
    if not earth_engine.initialize(
        config.earth_engine_service_account, config.earth_engine_private_key
    ):
      raise NotInitializedEarthEngineError()
    logging.info('Querying Open Buildings centroids. This may take a while.')
    earth_engine.get_open_buildings(
        regions,
        config.open_buildings_feature_collection,
        config.open_buildings_confidence,
        False,
        output_path,
    )
  else:
    raise ValueError('Invalid value for "buildings_method" flag.')


def _to_grayscale(image: np.ndarray) -> np.ndarray:
  return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def align_after_image(before_image: np.ndarray, after_image: np.ndarray):
  """Aligns after image to before image.

  Uses OpenCV template matching algorithm to align before and after
  images. Assumes that after_image is larger than before_image, so that the best
  alignment can be found. If the two images are the same size, then obviously no
  alignment is possible.

  Args:
    before_image: Before image.
    after_image: After image.

  Returns:
    A crop of after_image that is the same size as before_image and is best
    aligned to it.
  """
  result = cv2.matchTemplate(
      _to_grayscale(after_image), _to_grayscale(before_image),
      _ALIGNMENT_METHOD)
  _, _, _, max_location = cv2.minMaxLoc(result)
  j, i = max_location
  rows = before_image.shape[0]
  cols = before_image.shape[1]
  aligned_after = after_image[i:i + rows, j:j + cols, :]
  return aligned_after


def _mostly_blank(image: np.ndarray) -> bool:
  """Determines if an image is mostly blank.

  Assumes that the first dimension of the input data is the channel dimension. A
  pixel is considered blank if it has 0s in all channels.

  Args:
    image: Input image.

  Returns:
    Whether the image has too many blank pixels.
  """
  if image.size == 0:
    return 0

  flattened = image.max(axis=0)
  num_non_blank = np.count_nonzero(flattened)
  blank_fraction = (flattened.size - num_non_blank) / flattened.size
  return blank_fraction >= _BLANK_THRESHOLD


def _center_crop(image: np.ndarray, crop_size: int) -> np.ndarray:
  """Crops an image into a square of a specified size.

  Args:
    image: Input image array.
    crop_size: Length and width of the cropped image.

  Returns:
    The cropped image.
  """
  rows = image.shape[0]
  cols = image.shape[1]
  i = rows // 2 - crop_size // 2
  j = cols // 2 - crop_size // 2
  return image[i:i + crop_size, j:j + crop_size, :]


def _make_example_id(longitude: float, latitude: float, before_image_id: str,
                     after_image_id: str) -> str:
  """Hashes the uniquely identifying features of an example into a string id.

  Args:
    longitude: Longitude of example centroid.
    latitude: Latitude of example centroid.
    before_image_id: Id of before image.
    after_image_id: Id of after image.

  Returns:
    String hash of input features.
  """
  serialized = pickle.dumps(
      (longitude, latitude, before_image_id, after_image_id))
  return hashlib.md5(serialized).hexdigest()


def _make_int64_id(example_id: str) -> int:
  """Converts 128 bit hex string into 64 bit signed integer.

  Casts the first 64 bits of the hex string into an integer.

  Args:
    example_id: 128 bit hex string.

  Returns:
    64 bit signed integer.
  """
  return struct.unpack('<q', binascii.a2b_hex(example_id[:16]))[0]


class GenerateExamplesFn(beam.DoFn):
  """DoFn that extracts patches from before and after images into examples.

  The DoFn takes as input a list of (longitude, latitude) coordinates and
  extracts patches centered at each coordinate from the before and after images,
  and creates Tensorflow Examples containing these patches.

  The after image is also aligned to the before image during this process. The
  maximum displacement that can occur in alignment is _MAX_DISPLACEMENT pixels.

  Attributes:
    _large_patch_size: Size in pixels of the before and after image patches.
      Typically 256.
    _example_patch_size: Size in pixels of the smaller before and after image
      patches used in TF Examples. This is typically 64.
    _use_before_image: Whether to include before images in the examples.
  """

  def __init__(
      self,
      large_patch_size: int,
      example_patch_size: int,
      use_before_image: bool,
      cloud_detector_model_path: Optional[str] = None,
  ) -> None:
    self._cloud_detector_model_path = cloud_detector_model_path
    self._large_patch_size = large_patch_size
    self._example_patch_size = example_patch_size
    self._use_before_image = use_before_image

    self._example_count = Metrics.counter('skai', 'generated_examples_count')
    self._bad_example_count = Metrics.counter('skai', 'rejected_examples_count')
    self._before_patch_blank_count = Metrics.counter(
        'skai', 'before_patch_blank_count')
    self._after_patch_blank_count = Metrics.counter(
        'skai', 'after_patch_blank_count')

  def setup(self):
    if self._cloud_detector_model_path:
      self.cloud_detector = cloud_detector.CloudDetectorTFlite(
          self._cloud_detector_model_path
      )
    else:
      self.cloud_detector = None

  def _create_example(
      self,
      encoded_coordinates: str,
      before_image_id: str,
      before_image: np.ndarray,
      after_image_id: str,
      after_image: np.ndarray,
      scalar_features: Dict[str, List[Any]],
  ) -> Optional[Example]:
    """Create Tensorflow Example from inputs.

    Args:
      encoded_coordinates: Encoded coordinates.
      before_image_id: String identifier for before image.
      before_image: Before disaster image.
      after_image_id: String identifier for after image.
      after_image: After disaster image.
      scalar_features: Dict mapping scalar feature names to values.

    Returns:
      Tensorflow Example.
    """
    if self._use_before_image:
      after_image = align_after_image(before_image, after_image)
    before_crop = _center_crop(before_image, self._example_patch_size)
    if self._use_before_image and _mostly_blank(before_crop):
      self._before_patch_blank_count.inc()
      self._bad_example_count.inc()
      return None
    after_crop = _center_crop(after_image, self._example_patch_size)
    if _mostly_blank(after_crop):
      self._after_patch_blank_count.inc()
      self._bad_example_count.inc()
      return None

    example = Example()
    # TODO(jzxu): Use constants for these feature name strings.

    utils.add_bytes_feature(
        'encoded_coordinates', encoded_coordinates.encode(), example
    )
    longitude, latitude = scalar_features['coordinates']
    example_id = _make_example_id(
        longitude, latitude, before_image_id, after_image_id
    )
    int64_id = _make_int64_id(example_id)
    if 'plus_code' not in scalar_features:
      plus_code = openlocationcode.encode(
          latitude=latitude, longitude=longitude, codeLength=_PLUS_CODE_LENGTH
      )
      utils.add_bytes_feature('plus_code', plus_code.encode(), example)

    utils.add_bytes_feature('example_id', example_id.encode(), example)
    utils.add_int64_feature('int64_id', int64_id, example)
    utils.add_bytes_feature(
        'pre_image_png_large', tf.io.encode_png(before_image).numpy(), example
    )
    utils.add_bytes_feature(
        'pre_image_png', tf.io.encode_png(before_crop).numpy(), example
    )
    utils.add_bytes_feature('pre_image_id', before_image_id.encode(), example)
    utils.add_bytes_feature(
        'post_image_png_large', tf.io.encode_png(after_image).numpy(), example
    )
    utils.add_bytes_feature(
        'post_image_png', tf.io.encode_png(after_crop).numpy(), example
    )
    utils.add_bytes_feature('post_image_id', after_image_id.encode(), example)

    if self.cloud_detector:
      before_image_cloudiness = self.cloud_detector.detect_single(before_crop)
      after_image_cloudiness = self.cloud_detector.detect_single(after_crop)
      utils.add_float_feature(
          'before_image_cloudiness', before_image_cloudiness, example
      )
      utils.add_float_feature(
          'after_image_cloudiness', after_image_cloudiness, example
      )

    for name, value in scalar_features.items():
      if all(isinstance(v, bytes) for v in value):
        utils.add_bytes_list_feature(name, value, example)
      elif all(isinstance(v, str) for v in value):
        utils.add_bytes_list_feature(name, [v.encode() for v in value], example)
      elif all(isinstance(v, float) for v in value):
        utils.add_float_list_feature(name, value, example)
      elif all(isinstance(v, int) for v in value):
        utils.add_int64_list_feature(name, value, example)
      else:
        raise ValueError(f'Unknown value type for feature {name}.')
    return example

  def process(
      self, grouped_features: Tuple[str, Iterable[_FeatureUnion]]
  ) -> Iterator[Example]:
    """Extract patches from before and after images and output as tf Example.

    Args:
      grouped_features: Tuple of example id, list of features for that example.
        The elements of the features list are FeatureUnions that can be either
        scalar features or images.

    Yields:
      Serialized Tensorflow Example.
    """
    example_id, features = grouped_features
    before_images = []
    after_images = []
    scalar_features = {}
    for feature in features:
      if feature.scalar_features:
        scalar_features.update(feature.scalar_features)
      elif feature.before_image:
        before_images.append(feature.before_image)
      elif feature.after_image:
        after_images.append(feature.after_image)

    if not after_images:
      self._after_patch_blank_count.inc()
      self._bad_example_count.inc()
      return

    if self._use_before_image:
      if not before_images:
        self._before_patch_blank_count.inc()
        self._bad_example_count.inc()
        return
    else:
      before_image = np.zeros(
          (self._large_patch_size, self._large_patch_size, 3), dtype=np.uint8)
      before_images = [('', before_image)]

    for i, j in itertools.product(range(len(before_images)),
                                  range(len(after_images))):
      example = self._create_example(example_id, before_images[i][0],
                                     before_images[i][1], after_images[j][0],
                                     after_images[j][1], scalar_features)
      if example:
        self._example_count.inc()
        yield example


def _extract_scalar_features_from_buildings_file(buildings_path: str):
  """Extracts scalar features of each example from buildings file.

  Args:
    buildings_path: Path to serialized buildings file.

  Yields:
    Tuple of (encoded coordinates, scalar features dictionary).
  """
  buildings_gdf = buildings.read_buildings_file(buildings_path)
  for _, row in buildings_gdf.iterrows():
    longitude = row['longitude']
    latitude = row['latitude']
    label = row['label'] if 'label' in row.index else -1.0
    string_label = row['string_label'] if 'string_label' in row.index else ''
    encoded_coords = utils.encode_coordinates(longitude, latitude)
    scalar_features = {
        'coordinates': [longitude, latitude],
        'label': [label],
        'string_label': [string_label]
    }
    if 'full_plus_code' in row.index:
      scalar_features['plus_code'] = [row['full_plus_code']]
    if 'area_in_meters' in row.index:
      scalar_features['area_in_meters'] = [row['area_in_meters']]
    if row.geometry.type != 'Point':
      scalar_features['footprint_wkb'] = [shapely.wkb.dumps(row.geometry)]
    yield (encoded_coords, _FeatureUnion(scalar_features=scalar_features))


def _remove_large_images(example: Example) -> Example:
  new_example = Example()
  new_example.CopyFrom(example)
  del new_example.features.feature['pre_image_png_large']
  del new_example.features.feature['post_image_png_large']
  return new_example


def _expand_patterns(patterns: Iterable[str]) -> List[str]:
  """Returns the list of paths matched by a list of URI patterns.

  Args:
    patterns: List of file patterns.

  Returns:
    List of matched paths.
  """
  paths = []
  for pattern in patterns:
    if (pattern.startswith('/') or
        pattern.startswith('file://') or
        pattern.startswith('gs://') or
        pattern.startswith('s3://')):
      paths.extend(tf.io.gfile.glob(pattern))
    else:
      paths.append(pattern)
  return paths


def _generate_examples(
    pipeline,
    before_image_patterns: List[str],
    after_image_patterns: List[str],
    buildings_path: str,
    large_patch_size: int,
    example_patch_size: int,
    resolution: float,
    gdal_env: Dict[str, str],
    stage_prefix: str,
    cloud_detector_model_path: Optional[str] = None,
) -> Tuple[beam.PCollection, beam.PCollection]:
  """Generates examples and labeling images from source images.

  Args:
    pipeline: Beam pipeline.
    before_image_patterns: List of before image path patterns.
    after_image_patterns: List of after image path patterns.
    buildings_path: Path to serialized building footprints GeoDataFrame file.
    large_patch_size: Size in pixels of before and after image patches for
      labeling and alignment. Typically 256.
    example_patch_size: Size of patches to extract into examples. Typically 64.
    resolution: Desired resolution of image patches.
    gdal_env: GDAL environment configuration.
    stage_prefix: Beam stage name prefix.
    cloud_detector_model_path: Path to tflite cloud detector model.

  Returns:
    PCollection of examples and PCollection of labeling images.
  """
  scalar_features = (
      pipeline
      | stage_prefix + 'encode_buildings_path' >> beam.Create(
          [buildings_path])
      | stage_prefix + 'create_scalar_features' >> beam.FlatMap(
          _extract_scalar_features_from_buildings_file))

  input_collections = [scalar_features]
  after_image_size = large_patch_size
  use_before_image = bool(before_image_patterns)
  if use_before_image:
    # Make the after image patch larger than the before image patch by
    # giving it a border of _MAX_DISPLACEMENT pixels. This gives the
    # alignment algorithm at most +/-_MAX_DISPLACEMENT pixels of movement in
    # either dimension to find the best alignment.
    after_image_size += 2 * _MAX_DISPLACEMENT
    before_raster_paths = _expand_patterns(before_image_patterns)
    before_patches = read_raster.extract_patches_from_rasters(
        pipeline,
        buildings_path,
        before_raster_paths,
        large_patch_size,
        resolution,
        gdal_env,
        'before',
    )
    before_image_features = (
        before_patches
        | stage_prefix + '_before_to_feature' >> beam.MapTuple(
            lambda key, value: (key, _FeatureUnion(before_image=value))))
    input_collections.append(before_image_features)

  after_raster_paths = _expand_patterns(after_image_patterns)
  after_patches = read_raster.extract_patches_from_rasters(
      pipeline,
      buildings_path,
      after_raster_paths,
      after_image_size,
      resolution,
      gdal_env,
      'after',
  )
  after_image_features = (
      after_patches
      | stage_prefix + '_after_to_feature' >> beam.MapTuple(
          lambda key, value: (key, _FeatureUnion(after_image=value))))
  input_collections.append(after_image_features)

  examples = (
      input_collections
      | stage_prefix + '_merge_features' >> beam.Flatten()
      | stage_prefix + '_group_by_example_id' >> beam.GroupByKey()
      | stage_prefix + '_generate_examples'
      >> beam.ParDo(
          GenerateExamplesFn(
              large_patch_size,
              example_patch_size,
              use_before_image,
              cloud_detector_model_path,
          )
      )
  )

  return examples


def read_labels_file(
    path: str,
    label_property: str,
    labels_to_classes: List[str],
    max_points: int,
    output_path: str,
) -> None:
  """Reads labels from a GIS file and writes to the standard buildings format.

  If the "label_property" is a string, then it is assumed to be the name of a
  class, e.g. "damaged". In labels_to_classes, user can specify the mapping of
  the class and label, e.g. "damaged=1". If the name is not in
  "labels_to_classes", the example is dropped.

  If the label is a float or integer, it is read as-is without labels_to_classes
  specified.

  Args:
    path: Path to the file to be read.
    label_property: The property to use as the label, e.g. "string_label".
    labels_to_classes: List of string in "class=label" format, e.g.
      ["no_damage=0", "damaged=1", "destroyed=1"].
    max_points: Number of labeled examples to keep.
    output_path: Buildings file output path.
  """
  label_to_class_dict = {}
  for label_to_class in labels_to_classes:
    if '=' not in label_to_class:
      raise ValueError(
          f'Invalid label to class mapping "{label_to_class}",'
          f'should have form "label=class".')
    label, numeric_class = label_to_class.split('=')
    try:
      label_to_class_dict[label] = float(numeric_class)
    except TypeError:
      logging.error('Class %s is not numeric.', numeric_class)
      raise

  # Generate coordinates from label file
  gdf = gpd.read_file(path)
  if max_points:
    gdf = gdf.iloc[:max_points]
  string_labels = []
  float_labels = []
  for _, row in gdf.iterrows():
    label = row[label_property]
    if isinstance(label, str):
      try:
        string_labels.append(label)
        float_labels.append(label_to_class_dict[label])
      except KeyError:
        logging.warning('Label %s is not recognized.', label)
    elif isinstance(label, (int, float)):
      string_labels.append(str(label))
      float_labels.append(float(label))
    else:
      raise ValueError(f'Unrecognized label property type {type(label)}')

  output_gdf = gpd.GeoDataFrame(
      {'string_label': string_labels, 'label': float_labels},
      geometry=gdf.geometry,
  )
  buildings.write_buildings_file(output_gdf, output_path)


def parse_gdal_env(settings: List[str]) -> Dict[str, str]:
  """Parses a list of GDAL environment variable settings into a dictionary.

  Args:
    settings: A list of environment variable settings in "var=value" format.

  Returns:
    Dictionary with variable as key and assigned value.
  """
  gdal_env = {}
  for setting in settings:
    if '=' not in setting:
      raise ValueError(
          'Each GDAL environment setting should have the form "var=value".'
      )
    var, _, value = setting.partition('=')
    gdal_env[var] = value
  return gdal_env


def generate_examples_pipeline(
    before_image_patterns: List[str],
    after_image_patterns: List[str],
    large_patch_size: int,
    example_patch_size: int,
    resolution: float,
    output_dir: str,
    num_output_shards: int,
    buildings_path: str,
    buildings_labeled: bool,
    use_dataflow: bool,
    gdal_env: Dict[str, str],
    dataflow_job_name: Optional[str],
    cloud_project: Optional[str],
    cloud_region: Optional[str],
    worker_service_account: Optional[str],
    max_workers: int,
    wait_for_dataflow_job: bool,
    cloud_detector_model_path: Optional[str],
    output_metadata_file: bool) -> None:
  """Runs example generation pipeline.

  Args:
    before_image_patterns: Before image path patterns.
    after_image_patterns: After image path patterns.
    large_patch_size: Size in pixels of before and after image patches for
      labeling and alignment. Typically 256.
    example_patch_size: Size of patches to extract into examples. Typically 64.
    resolution: Desired resolution of image patches.
    output_dir: Parent output directory.
    num_output_shards: Number of output shards.
    buildings_path: Path to file containing building footprints.
    buildings_labeled: True if buildings have labels.
    use_dataflow: If true, run pipeline on GCP Dataflow.
    gdal_env: GDAL environment configuration.
    dataflow_job_name: Name of dataflow job.
    cloud_project: Cloud project name.
    cloud_region: Cloud region, e.g. us-central1.
    worker_service_account: Email of service account that will launch workers.
    max_workers: Maximum number of workers to use.
    wait_for_dataflow_job: If true, wait for dataflow job to complete before
      returning.
    cloud_detector_model_path: Path to tflite cloud detector model.
    output_metadata_file: Enable true to generate a file of example metadata, or
      disable to skip this step.
  """

  temp_dir = os.path.join(output_dir, 'temp')
  pipeline_options = beam_utils.get_pipeline_options(
      use_dataflow,
      dataflow_job_name,
      cloud_project,
      cloud_region,
      temp_dir,
      max_workers,
      worker_service_account,
      machine_type=None,
      accelerator=None,
      accelerator_count=0,
  )

  if buildings_labeled:
    examples_output_prefix = (
        os.path.join(output_dir, 'examples', 'labeled-large', 'labeled'))
  else:
    examples_output_prefix = (
        os.path.join(output_dir, 'examples', 'unlabeled-large', 'unlabeled'))

  pipeline = beam.Pipeline(options=pipeline_options)
  examples = _generate_examples(
      pipeline, before_image_patterns, after_image_patterns, buildings_path,
      large_patch_size, example_patch_size, resolution, gdal_env,
      'generate_examples', cloud_detector_model_path)

  _ = (
      examples
      | 'serialize_large_examples' >> beam.Map(
          lambda e: e.SerializeToString())
      | 'write_large_examples' >> beam.io.tfrecordio.WriteToTFRecord(
          examples_output_prefix,
          file_name_suffix='.tfrecord',
          num_shards=num_output_shards))

  if output_metadata_file:
    field_names = [
        'example_id',
        'encoded_coordinates',
        'longitude',
        'latitude',
        'post_image_id',
        'pre_image_id',
        'plus_code',
    ]
    _ = (
        examples
        | 'convert_metadata_examples_to_dict' >> beam.Map(_get_example_metadata)
        | 'combine_to_list' >> beam.combiners.ToList()
        | 'write_metadata_to_file'
        >> beam.ParDo(
            WriteMetadataToCSVFn(
                metadata_output_file_path=(
                    f'{output_dir}/examples/metadata_examples.csv'
                ), field_names=field_names
            )
        )
    )

  result = pipeline.run()
  if wait_for_dataflow_job:
    result.wait_until_finish()


class WriteMetadataToCSVFn(beam.DoFn):
  """DoFn to write meta data of examples to csv file.

  Attributes:
    metadata_output_file_path: File path to output meta data of all examples.
    field_names: Field names to be included in output file.
  """

  def __init__(self, metadata_output_file_path: str, field_names: List[str]):
    self.metadata_output_file_path = metadata_output_file_path
    self.field_names = field_names

  def process(self, element):
    with tf.io.gfile.GFile(
        self.metadata_output_file_path, 'w'
    ) as csv_output_file:
      csv_writer = csv.DictWriter(csv_output_file, fieldnames=self.field_names)
      csv_writer.writeheader()
      csv_writer.writerows(element)


def validate_image_patterns(
    image_patterns: List[str], check_for_empty: bool
) -> None:
  """Validates input image patterns.

  Checks if before_image_pattern and after_image_pattern occurs more than once
  or after_image_pattern is empty.

  Args:
    image_patterns: List containing image path patterns.
    check_for_empty: Boolean to check for empty image pattern if true.
  """
  duplicates = [
      pattern
      for pattern, pattern_count in collections.Counter(image_patterns).items()
      if pattern_count != 1
  ]

  if check_for_empty and not image_patterns:
    raise ValueError('No after_image_patterns specified.')

  elif duplicates:
    raise ValueError(
        'The following patterns occur more than once: '
        + ', '.join(sorted(duplicates))
    )


class ExampleType(typing.NamedTuple):
  example_id: str
  encoded_coordinates: str
  longitude: float
  latitude: float
  post_image_id: str
  pre_image_id: str
  plus_code: str


@beam.typehints.with_output_types(ExampleType)
def _get_example_metadata(example: tf.train.Example) -> ExampleType:
  example_id = utils.get_bytes_feature(example, 'example_id')[0].decode()
  encoded_coordinates = utils.get_bytes_feature(example, 'encoded_coordinates')[
      0
  ].decode()
  longitude, latitude = utils.get_float_feature(example, 'coordinates')
  post_image_id = utils.get_bytes_feature(example, 'post_image_id')[0].decode()
  pre_image_id = utils.get_bytes_feature(example, 'pre_image_id')[0].decode()
  plus_code = utils.get_bytes_feature(example, 'plus_code')[0].decode()

  return dict({
      'example_id': example_id,
      'encoded_coordinates': encoded_coordinates,
      'longitude': longitude,
      'latitude': latitude,
      'post_image_id': post_image_id,
      'pre_image_id': pre_image_id,
      'plus_code': plus_code,
  })
