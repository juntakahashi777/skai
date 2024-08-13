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

"""Functions for performing data labeling."""

import collections
import dataclasses
import functools
import multiprocessing
import os
import random
from typing import Any, Callable, Iterable, Optional

from absl import logging
import geopandas as gpd
import numpy as np
import pandas as pd
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
import scipy
from skai import utils
import tensorflow as tf


# Gap to add between before and after images.
BEFORE_AFTER_GAP = 10

# Margin around caption text.
CAPTION_MARGIN = 32

# Size of the reticule that identifies the building being labeled.
RETICULE_HALF_LEN = 32


Example = tf.train.Example
Image = PIL.Image.Image


@dataclasses.dataclass(frozen=True)
class LabelingExample:
  """Information about an example chosen for labeling.
  """
  int64_id: int
  example_id: str
  pre_image_path: str
  post_image_path: str
  combined_image_path: str
  tfrecord_path: str
  serialized_example: bytes
  longitude: float
  latitude: float


def _annotate_image(image: Image, caption: str) -> Image:
  """Adds center square and caption to image.

  Args:
    image: Input image.
    caption: Caption text to add.

  Returns:
    A copy of the input image with annotations added.
  """
  # Copy image into bigger frame to have room for caption.
  annotated_image = PIL.Image.new('RGB',
                                  (image.width, image.height + CAPTION_MARGIN),
                                  (225, 225, 225))
  annotated_image.paste(image, (0, 0))

  # Draw center rectangle.
  cx = image.width // 2
  cy = image.height // 2
  coords = [(cx - RETICULE_HALF_LEN, cy - RETICULE_HALF_LEN),
            (cx + RETICULE_HALF_LEN, cy + RETICULE_HALF_LEN)]
  annotations = PIL.ImageDraw.Draw(annotated_image)
  annotations.rectangle(coords, outline=(255, 0, 0), width=1)

  # Add caption.
  caption_xy = (cx, image.height + 5)
  annotations.text(caption_xy, caption, fill='black', anchor='mt')
  return annotated_image


def _read_example_ids_from_import_file(path: str) -> Iterable[str]:
  with tf.io.gfile.GFile(path, 'r') as import_file:
    df = pd.read_csv(import_file)
    return df['example_id']


def create_labeling_image(
    before_image: Image, after_image: Image, example_id: str, plus_code: str
) -> Image:
  """Creates an image used for labeling.

  The image is composed of the before and after images from the input example
  side-by-side.

  Args:
    before_image: Before image.
    after_image: After image.
    example_id: Example id.
    plus_code: Plus code.

  Returns:
    Annotated and combined image.

  """
  before_annotated = _annotate_image(before_image, 'BEFORE')
  after_annotated = _annotate_image(after_image, 'AFTER')
  width = before_annotated.width + after_annotated.width + 3 * BEFORE_AFTER_GAP
  height = before_annotated.height + 2 * BEFORE_AFTER_GAP
  combined = PIL.Image.new('RGB', (width, height), (225, 225, 225))
  combined.paste(before_annotated, (BEFORE_AFTER_GAP, BEFORE_AFTER_GAP))
  combined.paste(after_annotated,
                 (before_annotated.width + 2 * BEFORE_AFTER_GAP,
                  BEFORE_AFTER_GAP))
  caption = PIL.ImageDraw.Draw(combined)
  bottom_text = f'Example id: {example_id}   Plus code: {plus_code}'
  caption.text(
      (10, combined.height - 10),
      bottom_text,
      fill='black',
      anchor='lb',
  )
  return combined


def get_diffuse_subset(
    points: gpd.GeoDataFrame, buffer_meters: float
) -> gpd.GeoDataFrame:
  """Returns an arbitrary subset of points that are far away from each other.

    Points are kept or dropped based on the row order in the dataframe, so it's
    important for the input to already be randomly shuffled.

  Args:
    points: Points to drop neighbors from.
    buffer_meters: Buffer size in meters.

  Returns:
    Points with neighbors dropped.
  """
  buffer_df = gpd.GeoDataFrame(geometry=points.buffer(buffer_meters))
  joined = points.sjoin(buffer_df, how='left')
  indexes_to_keep = set()
  indexes_to_drop = set()
  for index, row in joined.iterrows():
    if index in indexes_to_drop:
      continue
    indexes_to_keep.add(index)
    if row.index_right != index:
      indexes_to_drop.add(row.index_right)
  assert len(indexes_to_keep) + len(indexes_to_drop) == len(points)
  assert indexes_to_keep.isdisjoint(indexes_to_drop)
  return points.loc[list(indexes_to_keep)]


def merge_dropping_neighbors(
    points: gpd.GeoDataFrame, new_points: gpd.GeoDataFrame, buffer_meters: float
) -> gpd.GeoDataFrame:
  """Merges new_points into points, dropping neighbors if necessary.

  Args:
    points: Points to merge into.
    new_points: New points to merge into points.
    buffer_meters: Buffer size in meters.

  Returns:
    Merged points.
  """
  buffer_df = gpd.GeoDataFrame(geometry=points.buffer(buffer_meters))
  joined = new_points.sjoin(buffer_df, how='inner')
  indexes_to_drop = list(set(joined.index))
  return pd.concat([points, new_points.drop(indexes_to_drop)])


def sample_with_buffer(
    points: gpd.GeoDataFrame,
    num_points: int,
    buffer_meters: float,
    starting_sample: Optional[gpd.GeoDataFrame] = None,
) -> gpd.GeoDataFrame:
  """Samples num_points from points, dropping neighbors if necessary.

  Args:
    points: Points to sample from.
    num_points: Number of points to sample.
    buffer_meters: Buffer size in meters.
    starting_sample: Points to start sampling from.

  Returns:
    GeoDataFrame containing sampled points.
  """
  unsampled = points.copy()
  if starting_sample is None:
    s = unsampled.sample(num_points)
    unsampled.drop(s.index, inplace=True)
    sample = get_diffuse_subset(s, buffer_meters)
    target_size = num_points
  else:
    target_size = len(starting_sample) + num_points
    sample = starting_sample.copy()
  while not unsampled.is_empty.all() and len(sample) < target_size:
    num_needed = target_size - len(sample)
    s = unsampled.sample(min(num_needed, len(unsampled)))
    unsampled.drop(s.index, inplace=True)
    s = get_diffuse_subset(s, buffer_meters)
    sample = merge_dropping_neighbors(sample, s, buffer_meters)
  return sample


def get_buffered_example_ids(
    examples_pattern: str,
    buffered_sampling_radius: float,
    excluded_example_ids: set[str],
    max_examples: Optional[int] = None,
) -> set[str]:
  """Gets a set of allowed example ids.

  Args:
    examples_pattern: File pattern for input TFRecords.
    buffered_sampling_radius: The minimum distance between two examples for the
      two examples to be in the labeling task.
    excluded_example_ids: Set of example ids to be excluded prior to generating
      buffered example ids.
    max_examples: Maximum number of images to create.
  Returns:
    Set of allowed example ids.
  """
  metadata_path = str(
      os.path.join(
          '/'.join(examples_pattern.split('/')[:-2]),
          'metadata_examples.csv',
      )
  )
  with tf.io.gfile.GFile(metadata_path, 'r') as f:
    try:
      df_metadata = pd.read_csv(f)
      df_metadata = df_metadata[
          ~df_metadata['example_id'].isin(excluded_example_ids)
      ].reset_index(drop=True)
    except tf.errors.NotFoundError as error:
      raise SystemExit(
          f'\ntf.errors.NotFoundError: {metadata_path} was not found\nUse'
          ' examples_to_csv module to generate metadata_examples.csv and/or'
          ' put metadata_examples.csv in the appropriate directory that is'
          ' PATH_DIR/examples/'
      ) from error

  logging.info(
      'Randomly searching for buffered samples with buffer radius %.2f'
      ' metres...',
      buffered_sampling_radius,
  )
  points = gpd.GeoSeries(
      gpd.points_from_xy(df_metadata['longitude'], df_metadata['latitude'])
  ).set_crs(4326)
  centroid = points.unary_union.centroid
  utm_points = points.to_crs(utils.convert_wgs_to_utm(centroid.x, centroid.y))
  gpd_df = gpd.GeoDataFrame(df_metadata, geometry=utm_points)
  max_examples = len(gpd_df) if max_examples is None else max_examples
  df_buffered_samples = sample_with_buffer(
      gpd_df, max_examples, buffered_sampling_radius
  )
  allowed_example_ids = set(df_buffered_samples['example_id'].unique())
  logging.info(
      'Allowing %d example ids with buffer radius: %.2f metres',
      len(allowed_example_ids),
      buffered_sampling_radius,
  )

  return allowed_example_ids


def create_labeling_images(
    examples_pattern: str,
    max_images: int,
    allowed_example_ids_path: str,
    excluded_import_file_patterns: list[str],
    output_dir: str,
    use_multiprocessing: bool,
    multiprocessing_context: Any,
    max_processes: int,
    buffered_sampling_radius: float,
    score_bins_to_sample_fraction: dict[tuple[float, float], float],
    scores_path: Optional[str] = None,
    filter_by_column: Optional[str] = None,
) -> int:
  """Samples labeling examples and creates PNG images for the labeling task.

  Example sampling works in 3 possible modes, depending on the parameters
  specified:
  - If allowed_example_ids_path is provided, then choose the examples with the
    example ids specified in this file. No random sampling occurs.
  - If scores_path is provided, then read the scores for examples from this file
    and sample the examples based on their scores. The rate at which different
    score ranges are sampled can be specified in the
    score_bins_to_sample_fraction parameter.
  - If neither of the above parameters are provided, then sample uniformly from
    all examples.

  For all 3 sampling modes, the parameter excluded_import_file_patterns can be
  provided to prevent sampling examples with specific example ids.

  This function will write 3 kinds of files to the output directory:
  - A set of PNG images for use in the labeling task. The files will be named
    after the example id of each example.
  - A text file containing the absolute paths of all the generated PNG files.
    This file can be used as the "import file" to specify which images to
    upload to the Vertex AI labeling service. This file will be named
    "import_file.csv", although it is not actually in CSV format, as it does not
    contain a header.
  - A CSV file with metadata information about each chosen example. The columns
    in this file are:
    - id: The int64 id of the example.
    - int64_id: The int64 id of the example (repeated for convenience).
    - example_id: The string example id of the example.
    - image: The path to the image file, in the format "file://<filepath>".
    - image_source_path: The path to the image file, without the "file://"
      prefix. Repeated for convenience.
    - tfrecord_source_path: Path to the TFRecord file that contains the example.

  Args:
    examples_pattern: File pattern for input TFRecords.
    max_images: Maximum number of images to create.
    allowed_example_ids_path: Path of file containing example ids that are
      allowed to be in the labeling set. The file should have one example id per
      line.
    excluded_import_file_patterns: List of import file patterns containing
      images to exclude.
    output_dir: Output directory.
    use_multiprocessing: If true, create multiple processes to create labeling
      images.
    multiprocessing_context: Context to spawn processes with when using
      multiprocessing.
    max_processes: Maximum number of processes.
    buffered_sampling_radius: The minimum distance between two examples for the
      two examples to be in the labeling task.
    score_bins_to_sample_fraction: Dictionary of bins for selecting labeling
      examples.
    scores_path: File containing scores obtained from pre-trained models.
    filter_by_column: If specified, the name of the column in the scores CSV
      file to use as a filter. The column must contain binary values, either
      true/false or 0/1. Rows with positive values in this column are then
      filtered out.

  Returns:
    Number of images written.
  """
  if scores_path and allowed_example_ids_path:
    raise ValueError(
        'scores_path and allowed_example_ids_path cannot be set at the same'
        ' time.'
    )
  example_files = tf.io.gfile.glob(examples_pattern)
  if not example_files:
    raise ValueError(
        f'Example pattern {examples_pattern} did not match any files.'
    )
  excluded_example_ids = set()
  if excluded_import_file_patterns:
    for pattern in excluded_import_file_patterns:
      for path in tf.io.gfile.glob(pattern):
        logging.info('Excluding example ids from "%s"', path)
        excluded_example_ids.update(_read_example_ids_from_import_file(path))
    logging.info('Excluding %d example ids', len(excluded_example_ids))

  if allowed_example_ids_path:
    with tf.io.gfile.GFile(allowed_example_ids_path, 'r') as f:
      allowed_example_ids = set(line.strip() for line in f)
    logging.info('Allowing %d example ids', len(allowed_example_ids))
    allowed_example_ids = allowed_example_ids - excluded_example_ids
  elif scores_path:
    allowed_example_ids = []
    with tf.io.gfile.GFile(scores_path, 'r') as f:
      scores_df = pd.read_csv(f)
      if 'damage_score' not in scores_df.columns:
        raise ValueError(f'damage_score column not found in {scores_path}')
      if filter_by_column:
        if filter_by_column not in scores_df.columns:
          raise ValueError(
              f'{filter_by_column} column not found in {scores_path}'
          )
        scores_df = scores_df[scores_df[filter_by_column] == 0]
      scores_df = scores_df[~scores_df['example_id'].isin(excluded_example_ids)]

    num_total_images = min(max_images, len(scores_df))
    for (
        low,
        high,
    ), percentage_examples in score_bins_to_sample_fraction.items():
      num_examples = int(percentage_examples * num_total_images)
      df_example_ids = scores_df[
          (scores_df['damage_score'] >= low)
          & (scores_df['damage_score'] < high)
      ]
      example_ids = list(df_example_ids['example_id'])
      random.shuffle(example_ids)
      allowed_example_ids.extend(example_ids[:num_examples])

  else:
    allowed_example_ids = get_buffered_example_ids(
        examples_pattern,
        buffered_sampling_radius,
        excluded_example_ids,
        max_images,
    )

  labeling_examples = _process_example_files(
      example_files,
      output_dir,
      use_multiprocessing,
      multiprocessing_context,
      max_processes,
      allowed_example_ids,
      _create_labeling_assets_from_example_file,
  )

  image_metadata_csv = os.path.join(
      output_dir, 'image_metadata.csv'
  )

  image_metadata_df = pd.DataFrame(
      [
          (
              i.int64_id,
              i.int64_id,
              i.example_id,
              f'file://{i.combined_image_path.replace("gs://", "/bigstore/")}',
              i.combined_image_path,
              i.pre_image_path,
              i.post_image_path,
              i.tfrecord_path,
              i.longitude,
              i.latitude,
          )
          for i in labeling_examples
      ],
      columns=(
          'id',
          'int64_id',
          'example_id',
          'image',
          'image_source_path',
          'pre_image_path',
          'post_image_path',
          'tfrecord_source_path',
          'longitude',
          'latitude',
      ),
  )
  with tf.io.gfile.GFile(image_metadata_csv, 'w') as f:
    image_metadata_df.to_csv(f, index=False)

  import_file_csv = os.path.join(output_dir, 'import_file.csv')
  with tf.io.gfile.GFile(import_file_csv, 'w') as f:
    f.write('\n'.join([e.combined_image_path for e in labeling_examples]))

  tfrecord_path = os.path.join(output_dir, 'labeling_examples.tfrecord')
  with tf.io.TFRecordWriter(tfrecord_path) as writer:
    for labeling_example in labeling_examples:
      writer.write(labeling_example.serialized_example)

  return len(labeling_examples)


def create_buffered_tfrecords(
    examples_pattern: str,
    output_dir: str,
    use_multiprocessing: bool,
    multiprocessing_context: Any,
    excluded_import_file_patterns: list[str],
    max_processes: int,
    buffered_sampling_radius: float,
):
  """Creates filtered TFRecords.

  Args:
    examples_pattern: File pattern for input TFRecords.
    output_dir: Output directory.
    use_multiprocessing: If true, create multiple processes to create labeling
      images.
    multiprocessing_context: Context to spawn processes with when using
      multiprocessing.
    excluded_import_file_patterns: List of import file patterns containing
      images to exclude.
    max_processes: Maximum number of processes.
    buffered_sampling_radius: The minimum distance between two examples for the
      two examples to be in the labeling task.

  """
  example_files = tf.io.gfile.glob(examples_pattern)
  if not example_files:
    raise ValueError(
        f'Example pattern {examples_pattern} did not match any files.'
    )

  excluded_example_ids = set()
  if excluded_import_file_patterns:
    for pattern in excluded_import_file_patterns:
      for path in tf.io.gfile.glob(pattern):
        logging.info('Excluding example ids from "%s"', path)
        excluded_example_ids.update(_read_example_ids_from_import_file(path))
    logging.info('Excluding %d example ids', len(excluded_example_ids))
  allowed_example_ids = get_buffered_example_ids(
      examples_pattern, buffered_sampling_radius, excluded_example_ids
  )

  _ = _process_example_files(
      example_files,
      output_dir,
      use_multiprocessing,
      multiprocessing_context,
      max_processes,
      allowed_example_ids,
      filter_examples_from_allowed_ids,
  )

  logging.info(
      'Filtered out %d images to tfrecords to %s',
      len(allowed_example_ids),
      output_dir,
  )


def _process_example_files(
    example_files: str,
    output_dir: str,
    use_multiprocessing: bool,
    multiprocessing_context: Any,
    max_processes: int,
    allowed_example_ids: set[str],
    processing_function: Callable[[str, str, set[str]], list[Any]],
) -> list[Any]:
  """Run a processing function on a list of files.

  Supports processing the files in parallel using multi-processing or
  sequentially.

  Args:
    example_files: List of input TFRecords.
    output_dir: Output directory.
    use_multiprocessing: If true, create multiple processes to create labeling
      images.
    multiprocessing_context: Context to spawn processes with when using
      multiprocessing.
    max_processes: Maximum number of processes.
    allowed_example_ids: Set of example_id from which a subset will be used in
      for filtering or creating a labeling file.
    processing_function: Function to be executed.

  Returns:
    Concatenated list of results from running the processing function on each
    file.
  """
  all_results = []
  if use_multiprocessing:
    def accumulate(results: list[Any]) -> None:
      all_results.extend(results)
    num_workers = min(
        multiprocessing.cpu_count(), len(example_files), max_processes
    )
    if multiprocessing_context:
      pool = multiprocessing_context.Pool(num_workers)
    else:
      pool = multiprocessing.Pool(num_workers)
    for example_file in example_files:
      pool.apply_async(
          processing_function,
          args=(example_file, output_dir, allowed_example_ids),
          callback=accumulate,
          error_callback=print,
      )
    pool.close()
    pool.join()
  else:
    for example_file in example_files:
      all_results.extend(processing_function(
          example_file, output_dir, allowed_example_ids
      ))

  return all_results


def _tfrecord_iterator(path: str) -> Example:
  """Creates an iterator over TFRecord files.

  Supports both eager and non-eager execution.

  Args:
    path: Path to TFRecord file.

  Yields:
    Examples from the TFRecord file.
  """
  ds = tf.data.TFRecordDataset([path]).prefetch(tf.data.AUTOTUNE)
  if tf.executing_eagerly():
    for record in ds:
      example = Example()
      example.ParseFromString(record.numpy())
      yield example
  else:
    iterator = tf.compat.v1.data.make_one_shot_iterator(ds)
    next_element = iterator.get_next()
    with tf.compat.v1.Session() as sess:
      while True:
        try:
          value = sess.run(next_element)
        except tf.errors.OutOfRangeError:
          return
        example = Example()
        example.ParseFromString(value)
        yield example


def _create_labeling_assets_from_example_file(
    example_file: str,
    output_dir: str,
    allowed_example_ids: set[str],
) -> list[LabelingExample]:
  """Creates assets needed for a labeling task from TFRecords.

  Creates combined and separate pre/post PNGs used by labeling tools. Also
  writes metadata CSV files needed by labeling tools to create labeling tasks.

  Args:
    example_file: Path to file containing TF records.
    output_dir: Directory to write assets to.
    allowed_example_ids: Set of example_id from which a subset will be used in
      creating labeling task.

  Returns:
    List of LabelingExamples containing information about the created assets.
  """
  labeling_examples = []
  for example in _tfrecord_iterator(example_file):
    if 'example_id' in example.features.feature:
      example_id = (
          example.features.feature['example_id'].bytes_list.value[0].decode()
      )
    else:
      # If the example doesn't have an "example_id" feature, fall back on using
      # "encoded_coordinates". This maintains backwards compatibility with
      # older datasets.
      # TODO(jzxu): Remove this branch when backward compatibility is no longer
      # needed.
      example_id = (
          example.features.feature['encoded_coordinates']
          .bytes_list.value[0]
          .decode()
      )

    if example_id not in allowed_example_ids:
      continue

    try:
      int64_id = utils.get_int64_feature(example, 'int64_id')[0]
    except IndexError as error:
      raise ValueError('Examples do not have int64_id feature') from error

    if 'plus_code' in example.features.feature:
      plus_code = (
          example.features.feature['plus_code'].bytes_list.value[0].decode()
      )
    else:
      plus_code = 'unknown'

    longitude, latitude = example.features.feature[
        'coordinates'
    ].float_list.value

    before_image = utils.deserialize_image(
        example.features.feature['pre_image_png_large'].bytes_list.value[0],
        'png',
    )
    after_image = utils.deserialize_image(
        example.features.feature['post_image_png_large'].bytes_list.value[0],
        'png',
    )
    combined_image = create_labeling_image(
        before_image, after_image, example_id, plus_code
    )

    pre_image_path = os.path.join(output_dir, f'{example_id}_pre.png')
    with tf.io.gfile.GFile(pre_image_path, 'wb') as f:
      f.write(
          example.features.feature['pre_image_png_large'].bytes_list.value[0]
      )
    post_image_path = os.path.join(output_dir, f'{example_id}_post.png')
    with tf.io.gfile.GFile(post_image_path, 'wb') as f:
      f.write(
          example.features.feature['post_image_png_large'].bytes_list.value[0]
      )
    combined_image_path = os.path.join(output_dir, f'{example_id}.png')
    with tf.io.gfile.GFile(combined_image_path, 'wb') as f:
      f.write(utils.serialize_image(combined_image, 'png'))

    labeling_examples.append(
        LabelingExample(
            int64_id=int64_id,
            example_id=str(example_id),
            pre_image_path=pre_image_path,
            post_image_path=post_image_path,
            combined_image_path=combined_image_path,
            tfrecord_path=example_file,
            serialized_example=example.SerializeToString(),
            longitude=longitude,
            latitude=latitude,
        )
    )

  return labeling_examples


def _write_tfrecord(examples: Iterable[Example], path: str) -> None:
  """Writes a list of examples to a TFRecord file."""
  output_dir = os.path.dirname(path)
  if not tf.io.gfile.exists(output_dir):
    tf.io.gfile.makedirs(output_dir)
  with tf.io.TFRecordWriter(path) as writer:
    for example in examples:
      writer.write(example.SerializeToString())


def filter_examples_from_allowed_ids(
    example_file: str, output_path: str, allowed_example_ids: set[str]
):
  """Filters examples based on allowed ids.

  Args:
    example_file: Path to file containing TF records.
    output_path: Path to output filtered examples.
    allowed_example_ids: List of example_id for filtering
  Returns:
    Empty list
  """
  filtered_examples = []
  for record in tf.data.TFRecordDataset([example_file]):
    example = Example()
    example.ParseFromString(record.numpy())
    if 'example_id' in example.features.feature:
      example_id = (
          example.features.feature['example_id'].bytes_list.value[0].decode()
      )
    else:
      # If the example doesn't have an "example_id" feature, fall back on
      # using "encoded_coordinates". This maintains backwards compatibility
      # with older datasets.
      # TODO(jzxu): Remove this branch when backward compatibility is no
      # longer needed.
      example_id = (
          example.features.feature['encoded_coordinates']
          .bytes_list.value[0]
          .decode()
      )
    if example_id in allowed_example_ids:
      filtered_examples.append(example)

  _write_tfrecord(
      filtered_examples, f'{output_path}/{example_file.split("/")[-1]}'
  )
  return []


def get_connection_matrix(
    longitudes: list[float],
    latitudes: list[float],
    encoded_coordinates: list[str],
    connecting_distance_meters: float,
)-> tuple[gpd.GeoDataFrame, np.ndarray]:
  """Gets a connection matrix for a set of points.

  Args:
    longitudes: Longitudes of points.
    latitudes: Latitudes of points.
    encoded_coordinates: Encoded coordinates of points.
    connecting_distance_meters: Maximum distance for two points to be connected.

  Returns:
    Tuple of (GeoDataFrame, connection_matrix).
  """
  points = gpd.GeoSeries(gpd.points_from_xy(
      longitudes,
      latitudes,
  )).set_crs(4326)

  centroid = points.unary_union.centroid
  utm_points = points.to_crs(utils.convert_wgs_to_utm(centroid.x, centroid.y))

  gpd_df = gpd.GeoDataFrame(
      {'encoded_coordinates': encoded_coordinates},
      geometry=utm_points
  )

  def calculate_euclidean_distance(row):
    return gpd_df.distance(row.geometry)

  distances = np.array(gpd_df.apply(calculate_euclidean_distance, axis=1))
  connection_matrix = (distances < connecting_distance_meters).astype('int')

  assert connection_matrix.shape == (
      len(encoded_coordinates),
      len(encoded_coordinates),
  )

  return gpd_df, connection_matrix


def get_connected_labels(
    connection_matrix: np.ndarray,
) -> list[str]:
  """Gets the labels of connected components.

  Args:
    connection_matrix: Connection matrix.

  Returns:
    List of labels of connected components. Components with the same label are
    connected and are therefore connected.
  """
  graph = scipy.sparse.csr_matrix(connection_matrix)
  _, labels = scipy.sparse.csgraph.connected_components(
      csgraph=graph, directed=False, return_labels=True
  )

  return list(labels)


def _split_examples(
    examples: list[Example],
    test_fraction: float,
    connecting_distance_meters: float,
) -> tuple[list[Example], list[Example]]:
  """Splits a list of examples into training and test sets.

  Examples with the same encoded coordinates will always end up in the same
  split to prevent leaking information between training and test sets. Any two
  examples separated by less than connecting_distance_meters will always be in
  the same split.

  Args:
    examples: Input examples.
    test_fraction: Fraction of examples to use for testing.
    connecting_distance_meters: Maximum distance for two points to be connected.

  Returns:
    Tuple of (training examples, test examples).
  """
  longitudes = []
  latitudes = []
  encoded_coordinates = []
  for example in examples:
    encoded_coordinates.append(utils.get_bytes_feature(
        example, 'encoded_coordinates'
    )[0].decode())
    longitude, latitude = utils.get_float_feature(example, 'coordinates')
    longitudes.append(longitude)
    latitudes.append(latitude)

  gpd_df, connection_matrix = get_connection_matrix(
      longitudes, latitudes, encoded_coordinates, connecting_distance_meters
  )
  labels = get_connected_labels(connection_matrix)
  connected_groups = collections.defaultdict(list)
  for idx, key in enumerate(labels):
    connected_groups[key].append(idx)

  list_of_connected_examples = []
  for _, connected_group in connected_groups.items():
    list_of_connected_examples.append(connected_group)

  num_test = int(len(gpd_df) * test_fraction)
  test_indices = get_testset_indices(num_test, list_of_connected_examples)
  test_examples = [examples[idx] for idx in test_indices]
  train_examples = [
      examples[idx] for idx in range(len(examples)) if idx not in test_indices
  ]

  return train_examples, test_examples


def get_testset_indices(num_test, list_of_connected_examples):
  """Get random list of indices corresponding to test examples.

  Args:
    num_test: Number of test examples.
    list_of_connected_examples: List of connected examples.

  Returns:
    List of indices corresponding test examples.
  """
  max_num_attempts_train_test_splits = 10000
  best_test_indices = []
  min_diff_best_num_test = num_test

  for _ in range(max_num_attempts_train_test_splits):
    # Ensure randomness
    random_list_of_connected_examples = random.sample(
        list_of_connected_examples, len(list_of_connected_examples)
    )
    current_test_indices = []

    for connected_component in random_list_of_connected_examples:
      current_test_indices.extend(connected_component)
      if abs(len(current_test_indices) - num_test) < min_diff_best_num_test:
        best_test_indices = current_test_indices.copy()
        min_diff_best_num_test = abs(len(best_test_indices) - num_test)

        # Stop trials once best best_test_indices is found
        if min_diff_best_num_test == 0:
          return best_test_indices

  return best_test_indices


def _merge_single_example_file_and_labels(
    example_file: str, labels: dict[str, list[tuple[str, float, str]]]
) -> list[Example]:
  """Merges TF records from single example_file with corresponding labels.

  Args:
    example_file: Path to file containing TF records.
    labels: Dictionary of example id to a list of tuples
        (string label, numeric label, source dataset id).

  Returns:
    List of TF examples merged with labels for a single example_file.
  """
  labeled_examples = []
  for example in _tfrecord_iterator(example_file):
    if 'example_id' in example.features.feature:
      example_id = (
          example.features.feature['example_id'].bytes_list.value[0].decode()
      )
    else:
      # If the example doesn't have an "example_id" feature, fall back on
      # using "encoded_coordinates". This maintains backwards compatibility
      # with older datasets.
      # TODO(jzxu): Remove this branch when backward compatibility is no
      # longer needed.
      example_id = (
          example.features.feature['encoded_coordinates']
          .bytes_list.value[0]
          .decode()
      )

    label_tuples = labels.get(example_id, [])
    for string_label, numeric_label, dataset_id_or_label_path in label_tuples:
      labeled_example = Example()
      labeled_example.CopyFrom(example)
      features = labeled_example.features
      features.feature['string_label'].bytes_list.value[:] = [
          string_label.encode()
      ]

      if tf.io.gfile.exists(dataset_id_or_label_path):
        features.feature['label_file_path'].bytes_list.value.append(
            dataset_id_or_label_path.encode()
        )
      else:
        features.feature['label_dataset_id'].bytes_list.value.append(
            dataset_id_or_label_path.encode()
        )

      label_feature = features.feature['label'].float_list
      if not label_feature.value:
        label_feature.value.append(numeric_label)
      else:
        label_feature.value[0] = numeric_label
      labeled_examples.append(labeled_example)

  return labeled_examples


def _match_patterns(patterns: list[str]) -> list[str]:
  paths = []
  for pattern in patterns:
    if not (matches := tf.io.gfile.glob(pattern)):
      raise ValueError(f'File pattern {pattern} did not match anything')
    paths.extend(matches)
  return paths


def _merge_examples_and_labels(
    example_patterns: list[str],
    labels: dict[str, list[tuple[str, float, str]]],
    use_multiprocessing: bool,
    multiprocessing_context: Any,
    max_processes: int,
) -> list[Example]:
  """Merges examples with labels into train and test TFRecords.

  Args:
    example_patterns: File patterns for input examples.
    labels: Dictionary of example ids to a list of tuples
        (string label, numeric label, source dataset id).
    use_multiprocessing: If true, create multiple processes to create labeled
      examples.
    multiprocessing_context: Context to spawn processes with when using
      multiprocessing.
    max_processes: Maximum number of processes.

  Returns:
    A list of labeled examples.
  """

  example_files = _match_patterns(example_patterns)
  if not labels:
    raise ValueError(
        'Dictionary of labels is empty. Ensure that the dictionary of'
        ' labels is not empty'
    )

  if use_multiprocessing:
    num_workers = min(
        multiprocessing.cpu_count(), len(example_files), max_processes
    )
    if multiprocessing_context:
      pool = multiprocessing_context.Pool(num_workers)
    else:
      pool = multiprocessing.Pool(num_workers)

    logging.info('Using multiprocessing with %d processes.', num_workers)
    results = pool.map(
        functools.partial(
            _merge_single_example_file_and_labels, labels=labels
        ),
        example_files,
    )
  else:
    logging.info('Not using multiprocessing.')
    results = [
        _merge_single_example_file_and_labels(example_file, labels)
        for example_file in example_files
    ]

  all_labeled_examples = []
  for result in results:
    all_labeled_examples.extend(result)
  return all_labeled_examples


def _read_label_file(path: str) -> list[tuple[str, str, str]]:
  """Reads a label file.

  The file should be a CSV containing at least an "example_id" column
  and a "string_label" column. In the future example_ids will also be supported.

  Args:
    path: Path to file.

  Returns:
    List of (example id, string label, label file path) tuples.
  """
  with tf.io.gfile.GFile(path) as f:
    df = pd.read_csv(f)

  if 'example_id' not in df.columns:
    raise ValueError('Label file must contain "example_id" column.')
  if 'string_label' not in df.columns:
    raise ValueError('Label file must contain "string_label" column.')

  return [(row.example_id, row.string_label, path) for _, row in df.iterrows()]


def create_labeled_examples(
    label_file_paths: list[str],
    string_to_numeric_labels: list[str],
    example_patterns: list[str],
    test_fraction: float,
    train_output_path: str,
    test_output_path: str,
    connecting_distance_meters: float,
    use_multiprocessing: bool,
    multiprocessing_context: Any,
    max_processes: int) -> None:
  """Creates a labeled dataset by merging cloud labels and unlabeled examples.

  Args:
    label_file_paths: Paths to files to read labels from.
    string_to_numeric_labels: List of strings in the form
      "<string label>=<numeric label>", e.g. "no_damage=0"
    example_patterns: Patterns for unlabeled examples.
    test_fraction: Fraction of examples to write to test output.
    train_output_path: Path to training examples TFRecord output.
    test_output_path: Path to test examples TFRecord output.
    connecting_distance_meters: Maximum distance for two points to be connected.
    use_multiprocessing: If true, create multiple processes to create labeled
      examples.
    multiprocessing_context: Context to spawn processes with when using
      multiprocessing.
    max_processes: Maximum number of processes.
  """
  string_to_numeric_map = {}
  for string_to_numeric_label in string_to_numeric_labels:
    if '=' not in string_to_numeric_label:
      raise ValueError(
          f'Invalid label mapping "{string_to_numeric_label}", should have '
          'form "label=0 or 1".')
    label, numeric_label = string_to_numeric_label.split('=')
    try:
      string_to_numeric_map[label] = float(numeric_label)
    except TypeError:
      logging.error('Class %s is not numeric.', numeric_label)
      raise

  if label_file_paths:
    labels = []
    for path in label_file_paths:
      labels.extend(_read_label_file(path))

    logging.info('Read %d labels total.', len(labels))
    ids_to_labels = collections.defaultdict(list)
    for example_id, string_label, dataset_id_or_label_path in labels:
      example_labels = ids_to_labels[example_id]
      if string_label in [l[0] for l in example_labels]:
        # Don't add multiple labels with the same value for a single example.
        continue
      numeric_label = string_to_numeric_map.get(string_label, None)
      if numeric_label is None:
        raise ValueError(f'Label "{string_label}" has no numeric mapping.')
      example_labels.append(
          (string_label, numeric_label, dataset_id_or_label_path)
      )

    all_labeled_examples = _merge_examples_and_labels(
        example_patterns,
        ids_to_labels,
        use_multiprocessing,
        multiprocessing_context,
        max_processes
    )
    if not all_labeled_examples:
      raise ValueError('No examples found matching labels.')
  else:
    # If no labels are provided, then assume that input examples already have
    # labels. Simply read them in.
    all_labeled_examples = []
    for example_path in _match_patterns(example_patterns):
      for example in _tfrecord_iterator(example_path):
        if ('string_label' not in example.features.feature) or (
            'label' not in example.features.feature
        ):
          raise ValueError(
              f'An example in file {example_path} does not have a string_label'
              ' or label feature.'
          )
        all_labeled_examples.append(example)

  train_examples, test_examples = _split_examples(
      all_labeled_examples, test_fraction, connecting_distance_meters
  )

  _write_tfrecord(train_examples, train_output_path)
  _write_tfrecord(test_examples, test_output_path)
  logging.info(
      'Written %d test examples and %d train examples',
      len(test_examples),
      len(train_examples),
  )
