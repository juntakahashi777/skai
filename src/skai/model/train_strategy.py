"""Train Strategy file.

This creates the strategy for specified accelerator, cpu, gpu or tpu.
"""
from typing import Union
import tensorflow as tf


_Strategy = Union[
  tf.distribute.Strategy,
  tf.distribute.MirroredStrategy,
  tf.distribute.TPUStrategy
    ]


def get_tpu_resolver(tpu='local'):
  """Create cluster resolver for Cloud TPUs
  Args:
    tpu: TPU to use - name, worker address or 'local'
  
  Returns:
    TPUClusterResolver for Cloud TPUs
  """
  resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu=tpu)
  tf.config.experimental_connect_to_cluster(resolver)
  tf.tpu.experimental.initialize_tpu_system(resolver)
  return resolver


def get_strategy(accelerator_type: str)->_Strategy:
  """Gets distributed training strategy for accelerator type
  Args:
    accelerator_type: The accelerator type which is one of cpu, gpu or tpu
  
  Returns:
    MirrorStrategy if accelerator_type is gpu,
        TPUStrategy if accelerator_type is tpu,
        else default Strategy
  """
  if accelerator_type == 'gpu':
    return tf.distribute.MirroredStrategy()
  elif accelerator_type == 'tpu':
    resolver = get_tpu_resolver()
    return tf.distribute.TPUStrategy(resolver)
  return tf.distribute.get_strategy()
