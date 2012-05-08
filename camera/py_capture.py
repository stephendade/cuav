#!/usr/bin/python

import chameleon, numpy, os, time, threading, Queue, cv, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'image'))
import scanner

from optparse import OptionParser
parser = OptionParser("py_capture.py [options] <filename>")
parser.add_option("--depth", type='int', default=16, help="image depth")
parser.add_option("--mono", action='store_true', default=False, help="use mono camera")
parser.add_option("--save", action='store_true', default=False, help="save images in tmp/")
parser.add_option("--compress", action='store_true', default=False, help="compress images for saving")
parser.add_option("--scan", action='store_true', default=False, help="run the Joe scanner")
parser.add_option("--num-frames", "-n", type='int', default=0, help="number of images to capture")
parser.add_option("--quality", type='int', default=95, help="compression quality")
parser.add_option("--brightness", type='int', default=100, help="auto-exposure brightness")
(opts, args) = parser.parse_args()

class capture_state():
  def __init__(self):
    self.save_thread = None
    self.scan_thread = None
    self.compress_thread = None
    self.bayer_thread = None
    self.bayer_queue = Queue.Queue()
    self.compress_queue = Queue.Queue()
    self.save_queue = Queue.Queue()

def timestamp(frame_time):
    '''return a localtime timestamp with 0.01 second resolution'''
    hundredths = int(frame_time * 100.0) % 100
    return "%s%02u" % (time.strftime("%Y%m%d%H%M%S", time.localtime(frame_time)), hundredths)

def start_thread(fn):
    '''start a thread running'''
    t = threading.Thread(target=fn)
    t.daemon = True
    t.start()
    return t

def get_base_time():
  '''we need to get a baseline time from the camera. To do that we trigger
  in single shot mode until we get a good image, and use the time we 
  triggered as the base time'''
  frame_time = None
  error_count = 0

  print('Opening camera')
  h = chameleon.open(not opts.mono, opts.depth, opts.brightness)

  while frame_time is None:
    try:
      base_time = time.time()
      im = numpy.zeros((960,1280),dtype='uint8' if opts.depth==8 else 'uint16')
      chameleon.trigger(h, False)
      frame_time, frame_counter, shutter = chameleon.capture(h, 1000, im)
      base_time -= frame_time
    except chameleon.error:
      print('failed to capture')
      error_count += 1
      if error_count > 3:
        error_count = 0
        print('re-opening camera')
        chameleon.close(h)
        h = chameleon.open(not opts.mono, opts.depth, opts.brightness)
  return h, base_time, frame_time


def save_thread():
  '''thread for saving files'''
  try:
    os.mkdir('tmp')
  except Exception:
    pass
  while True:
    frame_time, im, is_jpeg = state.save_queue.get()
    if is_jpeg:
      filename = 'tmp/i%s.jpg' % timestamp(frame_time)
      chameleon.save_file(filename, im)
    else:
      filename = 'tmp/i%s.pgm' % timestamp(frame_time)
      chameleon.save_pgm(filename, im)

def bayer_thread():
  '''thread for debayering images'''
  #img_full = cv.CreateImage((1280,960), 16, 1)
  #img8 = cv.CreateImage((1280,960), 8, 1)
  #im_colour = cv.CreateMat(960, 1280, cv.CV_8UC3)
  while True:
    frame_time, im = state.bayer_queue.get()
    # C debayer code
    im_colour = numpy.zeros((960,1280,3),dtype='uint8')
    scanner.debayer_16_full(im, im_colour)
    state.compress_queue.put((frame_time, im_colour))

    # OpenCV debayer code
    #cv.SetData(img_full, im.data)
    #cv.ConvertScale(img_full, img8, scale=1.0/256)
    #cv.CvtColor(img8, im_colour, cv.CV_BayerGR2BGR)
    #im2 = numpy.ascontiguousarray(im_colour)
    #state.compress_queue.put((frame_time, im2))


def compress_thread():
  '''thread for compressing images'''
  while True:
    frame_time, im = state.compress_queue.get()
    jpeg = scanner.jpeg_compress(im, int(opts.quality))
    if opts.save:
      state.save_queue.put((frame_time, jpeg, True))


def run_capture():
  '''the main capture loop'''

  print("Getting base frame time")
  h, base_time, last_frame_time = get_base_time()

  print('Starting continuous trigger mode')
  chameleon.trigger(h, True)
  
  frame_loss = 0
  num_captured = 0
  last_frame_counter = 0

  while True:
    im = numpy.zeros((960,1280),dtype='uint8' if opts.depth==8 else 'uint16')
    try:
      frame_time, frame_counter, shutter = chameleon.capture(h, 1000, im)
    except chameleon.error:
      print('failed to capture')
      continue
    if frame_time < last_frame_time:
      base_time += 128
    if last_frame_counter != 0:
      frame_loss += frame_counter - (last_frame_counter+1)

    if opts.compress:
      state.bayer_queue.put((base_time+frame_time, im))
    elif opts.save:
      state.save_queue.put((base_time+frame_time, im, False))

    print("Captured shutter=%f tdelta=%f ft=%f loss=%u qsave=%u qbayer=%u qcompress=%u" % (
        shutter, 
        frame_time - last_frame_time,
        frame_time,
        frame_loss,
        state.save_queue.qsize(),
        state.bayer_queue.qsize(),
        state.compress_queue.qsize()))

    last_frame_time = frame_time
    last_frame_counter = frame_counter
    num_captured += 1
    if num_captured == opts.num_frames:
      break

  print('Closing camera')
  time.sleep(2)
  chameleon.close(h)

# main program
state = capture_state()

if opts.save:
  state.save_thread = start_thread(save_thread)

if opts.compress:
  state.bayer_thread = start_thread(bayer_thread)
  state.compress_thread = start_thread(compress_thread)

run_capture()
