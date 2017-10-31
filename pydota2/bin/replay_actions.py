# NOTE: This code is to a large degree based on DeepMind work for 
#       AI in StarCraft2, just ported towards the Dota 2 game.
#       DeepMind's License is posted below.

#!/usr/bin/python
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dump out stats about all the actions that are in use in a set of replays."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import multiprocessing
import os
import signal
import sys
import threading
import time
import platform
import glob
import json

from future.builtins import range  # pylint: disable=redefined-builtin
import six
from six.moves import queue

replay_dir = 'replays'

from absl import app
from absl import flags

FLAGS = flags.FLAGS
flags.DEFINE_integer("parallel", 1, "How many instances to run in parallel.")
flags.DEFINE_integer("step_mul", 15, "How many game steps per observation.")
flags.DEFINE_string("replays", replay_dir, "Path to a directory of replays.")
flags.mark_flag_as_required("replays")

import pydota2.protobuf.CMsgBotWorldState_pb2 as _pb

def get_available_replays(path):
  d = os.path.join('.', path)
  return [os.path.join(d, o) for o in os.listdir(d) if os.path.isdir(os.path.join(d,o))]

def sorted_dict_str(d):
  return "{%s}" % ", ".join("%s: %s" % (k, d[k]) for k in sorted(d, key=d.get, reverse=True))

class ReplayStats(object):
  """Summary stats of the replays seen so far."""

  def __init__(self):
    self.replays = 0
    self.steps = 0

    #TODO - add other stats we want to track

    self.heroes = collections.defaultdict(int)
    self.unit_ids = collections.defaultdict(int)
    self.valid_abilities = collections.defaultdict(int)
    self.made_abilities = collections.defaultdict(int)
    self.valid_actions = collections.defaultdict(int)
    self.made_actions = collections.defaultdict(int)

    self.crashing_replays = set()
    self.invalid_replays = set()

  def merge(self, other):
    """Merge another ReplayStats into this one."""
    def merge_dict(a, b):
      for k, v in six.iteritems(b):
        a[k] += v

    self.replays += other.replays
    self.steps += other.steps

    #TODO - as above, add merging of other stats we track

    merge_dict(self.heroes, other.heroes)
    merge_dict(self.unit_ids, other.unit_ids)
    merge_dict(self.valid_abilities, other.valid_abilities)
    merge_dict(self.made_abilities, other.made_abilities)
    merge_dict(self.valid_actions, other.valid_actions)
    merge_dict(self.made_actions, other.made_actions)
    self.crashing_replays |= other.crashing_replays
    self.invalid_replays |= other.invalid_replays

  def __str__(self):
    len_sorted_dict = lambda s: (len(s), sorted_dict_str(s))
    len_sorted_list = lambda s: (len(s), sorted(s))
    return "\n\n".join((
        "Replays: %s, Steps total: %s" % (self.replays, self.steps),

        #TODO - print other stats we track

        "Heroes: %s\n%s" % len_sorted_dict(self.heroes),
        "Unit ids: %s\n%s" % len_sorted_dict(self.unit_ids),
        "Valid abilities: %s\n%s" % len_sorted_dict(self.valid_abilities),
        "Made abilities: %s\n%s" % len_sorted_dict(self.made_abilities),
        "Valid actions: %s\n%s" % len_sorted_dict(self.valid_actions),
        "Made actions: %s\n%s" % len_sorted_dict(self.made_actions),
        "Crashing replays: %s\n%s" % len_sorted_list(self.crashing_replays),
        "Invalid replays: %s\n%s" % len_sorted_list(self.invalid_replays),
    ))

class ProcessStats(object):
  """Stats for a worker process."""

  def __init__(self, proc_id):
    self.proc_id = proc_id
    self.time = time.time()
    self.stage = ""
    self.replay = ""
    self.replay_stats = ReplayStats()

  def update(self, stage):
    self.time = time.time()
    self.stage = stage

  def __str__(self):
    return ("[%2d] replay: %10s, replays: %5d, steps: %7d, game loops: %7s, "
            "last: %12s, %3d s ago" % (
                self.proc_id, self.replay, self.replay_stats.replays,
                self.replay_stats.steps,
                self.replay_stats.steps * FLAGS.step_mul, self.stage,
                time.time() - self.time))

def valid_replay(info):
  """Make sure the replay isn't corrupt, and is worth looking at."""
  #TODO - figure out what metrics to use to determine if either
  # an error occurred or it's just a low-level MMR replay not
  # worth learning from
  return True

class ReplayProcessor(multiprocessing.Process):
  """A Process that pulls replays and processes them."""

  def __init__(self, proc_id, replay_queue, stats_queue):
    super(ReplayProcessor, self).__init__()
    self.stats = ProcessStats(proc_id)
    self.replay_queue = replay_queue
    self.stats_queue = stats_queue

  def run(self):
    signal.signal(signal.SIGTERM, lambda a, b: sys.exit())  # Exit quietly.
    self._update_stage("spawn")
    replay_name = "none"
    while True:
      self._print("Starting up a new Dota2 replay instance.")
      self._update_stage("launch")
      try:
        for _ in range(300):
          try:
            replay_path = self.replay_queue.get()
          except queue.Empty:
            self._update_stage("done")
            self._print("Empty queue, returning")
            return
          try:
            replay_name = os.path.basename(replay_path)
            self.stats.replay = replay_name
            self._print("Got replay: '%s'" % replay_path)
            self._update_stage("open replay directory")
            #TODO - process the replay info (total game time, winner, timestep interval)
            replay_data = self.load_replay(replay_path)
            replay_info = self.summarize_replay(replay_data)
            self._print((" Replay Info %s " % replay_name).center(60, "-"))
            self._print(replay_info)
            self._print("-" * 60)
            if valid_replay(replay_info):
              self._update_stage("process replay")
              self.process_replay(replay_data, replay_info['team_id'])
          finally:
            self.replay_queue.task_done()
        self._update_stage("shutdown")
      except KeyboardInterrupt:
        return
      except:
        print("[Run Replay] Unexpected error:", sys.exc_info()[0])
        self.stats.replay_stats.crashing_replays.add(replay_name)
        raise

  def load_replay(self, replay_path):
    """Load the replay data into memory through time-ordered JSON objects."""
    self._update_stage("loading replay into memory")
    
    files = sorted(glob.glob(os.path.join(replay_path, '*.bin')))

    data = {}
    indx = 0
    for fname in [files[0], files[-1]]:
      #indx = int(os.path.basename(fname[:-4]))
      #print("Loading Protobuf file: %d" % indx)
      try:
        proto_file = open(fname, 'rb')
        data_frame = _pb.CMsgBotWorldState()
        data_frame.ParseFromString(proto_file.read())
        data[indx] = data_frame
        proto_file.close()
      except Exception as e:
        print('Protobuf loading error: %s for file %s' % (str(e), fname))
        break
      indx += 1

    return data

  def summarize_replay(self, replay_data):
    """Summarize the replay (length of time, winner, heroes, roles)."""
    self._update_stage("summarizing replay")

    info = {}

    try:
      print('Replay Length: %d' % (len(replay_data)))
      info['game_length'] = replay_data[len(replay_data)-1].game_time - replay_data[0].game_time
      info['team_id'] = replay_data[0].team_id

      for unit in replay_data[len(replay_data)-1].units:
        if unit.unit_type == 9:
            info['ancient_hp_' + str(unit.team_id)] = unit.health
    except:
      print("[Summarize Replay] Unexpected error:", sys.exc_info()[0])
      print(replay_data[0])
      print(replay_data[len(replay_data)-1])
      raise
    
    return info

  def process_replay(self, replay_data, team_id):
    """Process a single replay, updating the stats."""
    self._update_stage("start_replay")

    self.stats.replay_stats.replays += 1
    for step, data in replay_data.items():
      self.stats.replay_stats.steps += 1
      self._update_stage('Step %d of %d - Observe' % (step, len(replay_data)-1))
      
      # TODO - complete the actual Reinforcement Learning

  def _print(self, s):
    for line in str(s).strip().splitlines():
      print("[%s] %s" % (self.stats.proc_id, line))
    sys.stdout.flush()

  def _update_stage(self, stage):
    self.stats.update(stage)
    self.stats_queue.put(self.stats)

def stats_printer(stats_queue):
  """A thread that consumes stats_queue and prints them every 10 seconds."""
  proc_stats = [ProcessStats(i) for i in range(FLAGS.parallel)]
  print_time = start_time = time.time()
  width = 107

  running = True
  while running:
    print_time += 10

    while time.time() < print_time:
      try:
        s = stats_queue.get(True, print_time - time.time())
        if s is None:  # Signal to print and exit NOW!
          running = False
          break
        proc_stats[s.proc_id] = s
      except queue.Empty:
        pass

    replay_stats = ReplayStats()
    for s in proc_stats:
      replay_stats.merge(s.replay_stats)

    print((" Summary %0d secs " % (print_time - start_time)).center(width, "="))
    print(replay_stats)
    print(" Process stats ".center(width, "-"))
    print("\n".join(str(s) for s in proc_stats))
    print("=" * width)

def replay_queue_filler(replay_queue, replay_list):
  """A thread that fills the replay_queue with replay filenames."""
  for replay_path in replay_list:
    replay_queue.put(replay_path)

def main(unused_argv):
  stats_queue = multiprocessing.Queue()
  stats_thread = threading.Thread(target=stats_printer, args=(stats_queue,))
  stats_thread.start()

  FLAGS.replays = get_available_replays(replay_dir)
  #print(FLAGS.replays)

  try:
    # For some reason buffering everything into a JoinableQueue makes the
    # program not exit, so save it into a list then slowly fill it into the
    # queue in a separate thread. Grab the list synchronously so we know there
    # is work in the queue before the Dota2 processes actually run, otherwise
    # The replay_queue.join below succeeds without doing any work, and exits.
    print("Getting replay list:", FLAGS.replays)
    replay_list = sorted(FLAGS.replays)
    print(len(replay_list), "replays found.\n")
    replay_queue = multiprocessing.JoinableQueue(FLAGS.parallel * 10)
    replay_queue_thread = threading.Thread(target=replay_queue_filler,
                                           args=(replay_queue, replay_list))
    replay_queue_thread.daemon = True
    replay_queue_thread.start()

    for i in range(FLAGS.parallel):
      p = ReplayProcessor(i, replay_queue, stats_queue)
      p.daemon = True
      p.start()
      time.sleep(1)  # Stagger startups, otherwise they seem to conflict somehow

    replay_queue.join()  # Wait for the queue to empty.
  except KeyboardInterrupt:
    print("Caught KeyboardInterrupt, exiting.")
  finally:
    stats_queue.put(None)  # Tell the stats_thread to print and exit.
    stats_thread.join()


if __name__ == "__main__":
  app.run(main)
