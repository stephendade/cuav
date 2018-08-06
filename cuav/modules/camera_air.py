#!/usr/bin/env python
'''realtime imaging control via MAVProxy, air side
It takes in captured images from a directory and scans
them in realtime, geotags and sends to the GCS'''

#Notes:
#-There are 3 threads - capture, scan and transmit
#-There are 2 queues - scan_queue, transmit_queue

# todo:
#    - add ability to lower score and get past images sent

import time, threading, sys, os, numpy, Queue, cPickle, cStringIO
import functools, cv2, pkg_resources

from MAVProxy.modules.lib import mp_module

from cuav.image import scanner
from cuav.lib import mav_position, cuav_util, cuav_joe, block_xmit, cuav_region, cuav_command
from MAVProxy.modules.lib import mp_settings
from cuav.camera.cam_params import CameraParams


class CameraAirModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super(CameraAirModule, self).__init__(mpstate, "camera_air", "cuav camera control (air)", public = True)

        self.running = False
        self.unload_event = threading.Event()
        self.unload_event.clear()

        self.capture_thread = None
        self.scan_thread = None
        self.transmit_thread = None
        self.airstart_triggered = False
        self.terrain_alt = None
        self.handled_timestamps = {}
        self.imagefilenamemapping = {}

        # prevent loopback of messages
        #for mtype in ['DATA16', 'DATA32', 'DATA64', 'DATA96']:
        #    self.module('link').no_fwd_types.add(mtype)

        from MAVProxy.modules.lib.mp_settings import MPSettings, MPSetting
        self.camera_settings = MPSettings(
            [ MPSetting('roll_stabilised', bool, False, 'Roll Stabilised'),
              MPSetting('roll_limit', float, 30, 'Roll stabilisation limit'),
              MPSetting('minspeed', int, 20, 'For airstart, minimum speed for capture to start'),
              MPSetting('minalt', int, 30, 'MinAltitude of images', range=(0,10000), increment=1),
              MPSetting('rotate180', bool, False, 'rotate images by 180', tab='Capture2'),
              MPSetting('ignoretimestamps', bool, False, 'Ignore image timestamps', tab='Capture2'),
              MPSetting('camparms', str, None, 'camera parameters file (json) in cuav package', tab='Imaging'),
              MPSetting('imagefile', str, None, 'latest captured image', tab='Imaging'),
              MPSetting('filter_type', str, 'simple', 'Filter Type',
                        choice=['simple'], tab='Imaging'),
              MPSetting('blue_emphasis', bool, False, 'BlueEmphasis', tab='Imaging'),
              MPSetting('use_capture_time', bool, True, 'Use Capture Time (false for sim)', tab='Simulation'),
              MPSetting('target_latitude', float, 0, 'filter detected images to latitude', tab='Filter to Location'),
              MPSetting('target_longitude', float, 0, 'filter detected images to longitude', tab='Filter to Location'),
              MPSetting('target_radius', float, 0, 'filter detected images to radius', tab='Filter to Location'),

              MPSetting('gcs_address', str, "", 'GCS Addresses in RemIP:RemPort:LocalPort:Bandwidth format (127.0.0.1:1440:1234:45, ...)', tab='GCS'),
              MPSetting('qualitysend', int, 90, 'Compression Quality for send', range=(1,100), increment=1, tab='GCS'),
              MPSetting('transmit', bool, True, 'Transmit Enable for thumbnails', tab='GCS'),
              MPSetting('maxqueue', int, 100, 'Maximum images queue', tab='GCS'),

              MPSetting('thumbsize', int, 60, 'Thumbnail Size', range=(10, 200), increment=1),
              MPSetting('minscore', int, 400, 'Min Score to pass detection', range=(0,5000), increment=1, tab='Imaging'),
              MPSetting('clock_sync', bool, False, 'GPS Clock Sync'),
              ],
            title='Camera Settings'
            )

        self.image_settings = MPSettings(
            [ MPSetting('MinRegionArea', float, 0.15, range=(0,100), increment=0.05, digits=2, tab='Image Processing'),
              MPSetting('MaxRegionArea', float, 1.0, range=(0,100), increment=0.1, digits=1, tab='Image Processing'),
              MPSetting('MinRegionSize', float, 0.2, range=(0,100), increment=0.05, digits=2, tab='Image Processing'),
              MPSetting('MaxRegionSize', float, 1.0, range=(0,100), increment=0.1, digits=1, tab='Image Processing'),
              MPSetting('MaxRarityPct',  float, 0.02, range=(0,100), increment=0.01, digits=2, tab='Image Processing'),
              MPSetting('RegionMergeSize', float, 1.0, range=(0,100), increment=0.1, digits=1, tab='Image Processing'),
              ],
            title='Image Settings')

        self.capture_count = 0
        self.scan_count = 0
        self.error_count = 0
        self.error_msg = None
        self.region_count = 0
        self.scan_fps = 0
        self.scan_queue = Queue.Queue()
        self.transmit_queue = Queue.Queue()
        self.have_set_gps_time = False

        self.c_params = None
        self.jpeg_size = 0
        self.xmit_queue = []
        self.efficiency = []

        self.last_watch = 0
        self.boundary = None
        self.boundary_polygon = None

        self.bandwidth_used = []
        self.rtt_estimate = []
        self.bsend = [] #note this is an array of bsends
        self.last_heartbeat = time.time()

        self.mpos = mav_position.MavInterpolator(backlog=5000, gps_lag=0.0)
        self.joelog = None #cuav_joe.JoeLog(os.path.join(self.settings.imagefile, '..', 'joe.log'), append=self.continue_mode)

        self.add_command('camera', self.cmd_camera,
                         'camera control',
                         ['<start|stop|status|boundary|airstart>',
                          'set (CAMERASETTING)'])
        self.add_completion_function('(CAMERASETTING)', self.settings.completion)
        self.add_completion_function('(CAMERASETTING)', self.camera_settings.completion)
        print("camera initialised")

    def cmd_camera(self, args):
        '''camera commands'''
        usage = "usage: camera <start|airstart|stop|status|queue|set>"
        if len(args) == 0:
            print(usage)
            return
        if args[0] == "start":
            self.capture_count = 0
            self.error_count = 0
            self.error_msg = None
            #check cam params
            if not self.check_camera_parms():
                print("Error - incorrect camera params " + str(self.camera_settings.camparms))
                return
            if self.running == False:
                self.running = True
                self.joelog = cuav_joe.JoeLog(os.path.join(os.path.dirname(self.camera_settings.imagefile), 'joe_air.log'), append=self.continue_mode)
                self.capture_thread = self.start_thread(self.capture_threadfunc)
                self.scan_thread = self.start_thread(self.scan_threadfunc)
                self.transmit_thread = self.start_thread(self.transmit_threadfunc)
                time.sleep(0.1)
                self.send_message("Started cuav running")
                print("Started cuav running")
            else:
                self.send_message("cuav already running")
                print("cuav already running")
        elif args[0] == "stop":
            self.send_message("Stopped cuav")
            self.running = False
            self.airstart_triggered = False
            print("Stopped cuav")
        elif args[0] == "status":
            ret = "Cap imgs:%u err:%u scan:%u regions:%u jsize:%.0f xmitq:%s sq:%.1f eff:%s" % (
                self.capture_count, self.error_count, self.scan_count,
                self.region_count,
                self.jpeg_size,
                self.xmit_queue, self.scan_queue.qsize(),
                self.efficiency)
            print(ret)
            self.send_message(ret)
        elif args[0] == "queue":
            ret = "scan %u  transmit %u  eff %s  bw %s  rtt %s" % (
                self.scan_queue.qsize(),
                self.transmit_queue.qsize(),
                self.efficiency,
                self.bandwidth_used,
                self.rtt_estimate)
            print(ret)
            self.send_message(ret)
        elif args[0] == "set":
            self.camera_settings.command(args[1:])
        elif args[0] == "airstart":
            #just keep the block xmit going for now
            self.capture_count = 0
            self.error_count = 0
            self.error_msg = None
            #check cam params
            if not self.check_camera_parms():
                print("Error - incorrect camera params " + str(self.camera_settings.camparms))
                return
            if self.airstart_triggered == False:
                self.airstart_triggered = True
                self.joelog = cuav_joe.JoeLog(os.path.join(os.path.dirname(self.camera_settings.imagefile), 'joe_air.log'), append=self.continue_mode)
                self.transmit_thread = self.start_thread(self.transmit_threadfunc)
                time.sleep(0.1)
                self.send_message("cuav airstart ready")
                print("cuav airstart ready")
            else:
                self.send_message("cuav airstart already running")
                print("cuav airstart already running")
        else:
            print(usage)

    def check_camera_parms(self):
        '''check for change in camera parameters'''
        #dir is rel to this python file:
        if self.camera_settings.camparms is None:
            return False
        camfiletxt = pkg_resources.resource_string("cuav", self.camera_settings.camparms)
        try:
            self.c_params = CameraParams.fromstring(camfiletxt)
            return True
        except:
            return False

    def capture_threadfunc(self):
        '''image capture thread, via monitoring the
        link for changed linked filenames'''
        prev_image = None
        self.scan_queue = Queue.Queue()
        while not self.unload_event.wait(0.05):
            try:
                filename = os.path.realpath(self.camera_settings.imagefile)
                if not self.camera_settings.ignoretimestamps:
                    filetime = cuav_util.parse_frame_time(filename)
                else:
                    filetime = float(time.time())
            except Exception:
                filename = None
                pass
            #ensure all items are valid and the queue isn't overfilled > 100
            if filename != None and prev_image != filename and filetime != None and self.scan_queue.qsize() < 100:
                self.scan_queue.put((filetime, filename))
                self.imagefilenamemapping[str(filetime)] = filename
                self.capture_count += 1
                prev_image = filename

    def scan_threadfunc(self):
        '''image scanning thread'''
        while not self.unload_event.wait(0.05):
            try:
                (frame_time,im) = self.scan_queue.get()
            except Queue.Empty:
                continue
            scan_parms = {}
            for name in self.image_settings.list():
                scan_parms[name] = self.image_settings.get(name)
            scan_parms['BlueEmphasis'] = float(self.camera_settings.blue_emphasis)

            if self.terrain_alt is not None:
                altitude = self.terrain_alt
                if altitude < self.camera_settings.minalt:
                    altitude = self.camera_settings.minalt
                scan_parms['MetersPerPixel'] = cuav_util.pixel_width(altitude,
                                                                     self.c_params.xresolution,
                                                                     self.c_params.lens,
                                                                     self.c_params.sensorwidth)

            t1 = time.time()
            img_scan = cv2.imread(im, -1)
            if self.camera_settings.rotate180:
                M = cv2.getRotationMatrix2D(center, angle180, scale)
                (h, w) = img_scan.shape[:2]
                img_scan = cv2.warpAffine(img_scan, M, (w, h))
            im_numpy = numpy.ascontiguousarray(img_scan)
            regions = scanner.scan(im_numpy, scan_parms)
            regions = cuav_region.RegionsConvert(regions,
                                                 cuav_util.image_shape(img_scan),
                                                 cuav_util.image_shape(img_scan))
            t2 = time.time()
            self.scan_fps = 1.0 / (t2-t1)
            self.scan_count += 1

            regions = cuav_region.filter_regions(img_scan, regions,
                                                 min_score=self.camera_settings.minscore,
                                                 filter_type=self.camera_settings.filter_type)
            self.region_count += len(regions)
            
            if self.camera_settings.roll_stabilised:
                roll=0
            else:
                roll=None
            pos = self.get_plane_position(frame_time, roll=roll)

            # this adds the latlon field to the regions
            if self.joelog:
                self.log_joe_position(pos, frame_time, regions)

            # filter out any regions outside the target radius
            if self.camera_settings.target_radius > 0 and pos is not None:
                regions = cuav_region.filter_radius(regions,
                                                    (self.camera_settings.target_latitude,
                                                     self.camera_settings.target_longitude),
                                                    self.camera_settings.target_radius)

            # filter out any regions outside the boundary
            if self.boundary_polygon:
                regions = cuav_region.filter_boundary(regions, self.boundary_polygon, pos)
                regions = cuav_region.filter_regions(img_scan, regions, min_score=self.camera_settings.minscore,
                                                     filter_type=self.camera_settings.filter_type)

            if len(regions) > 0:
                lowscore = 0
                highscore = 0
                for r in regions:
                    lowscore = min(lowscore, r.score)
                    highscore = max(highscore, r.score)

                if self.camera_settings.transmit:
                    # send a region message with thumbnails to the ground station
                    thumb_img = cuav_region.CompositeThumbnail(img_scan, regions,
                                                               thumb_size=self.camera_settings.thumbsize)
                    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
                    (result, thumb) = cv2.imencode('.jpg', thumb_img, encode_param)
                    pkt = cuav_command.ThumbPacket(frame_time, regions, thumb, pos, highscore)

                    # keep all thumbs so we can send more on score change
                    self.add_all_thumbs(pkt)

                    if self.camera_settings.transmit and highscore >= self.camera_settings.minscore:
                        if self.transmit_queue.qsize() < 100:
                            self.transmit_queue.put((pkt, None, None))
                        else:
                            self.send_message("Warning: image Tx queue too long")
                            print("Warning: image Tx queue too long")

    def get_plane_position(self, frame_time,roll=None):
        '''get a MavPosition object for the planes position if possible'''
        try:
            pos = self.mpos.position(frame_time, 0, roll=roll, maxroll=self.camera_settings.roll_limit)
            return pos
        except mav_position.MavInterpolatorException as e:
            print str(e)
            return None

    def log_joe_position(self, pos, frame_time, regions, filename=None, thumb_filename=None):
        '''add to joe.log if possible, returning a list of (lat,lon) tuples
        for the positions of the identified image regions'''
        return self.joelog.add_regions(frame_time, regions, pos, filename,
                                       thumb_filename, altitude=None, C=self.c_params)


    def add_all_thumbs(self, pkt):
        '''add to all_thumbs list'''
        if pkt.pos is None or pkt.pos.altitude > 20:
            # don't save ground photos
            return

    def send_heartbeats(self):
        '''possibly send heartbeat msgs'''
        now = time.time()
        if now - self.last_heartbeat > 5:
            self.last_heartbeat = now
            self.send_heartbeat()

    def transmit_threadfunc(self):
        '''thread for image and message transmit to camera_ground
        in addition to reading commands from the camera_ground'''
        self.start_aircraft_bsend()
        self.spacewarning = False

        while (not self.unload_event.wait(0.05)) or self.airstart_triggered:
            for bsnd in self.bsend:
                bsnd.tick(packet_count=1000, max_queue=self.camera_settings.maxqueue)
                self.check_commands(bsnd)
            self.send_heartbeats()

            #check remaining disk space and warn user if required
            try:
                stat = os.statvfs(os.path.dirname(self.camera_settings.imagefile))
                if not self.spacewarning and stat.f_bfree*stat.f_bsize < 20971520:
                    self.send_message("Warning: <200Mb disk space left on cuav_air")
                    self.spacewarning = True
            except OSError:
                pass

            while not self.transmit_queue.empty():
                (pkt, priority, linktosend) = self.transmit_queue.get()
                self.send_object(pkt, priority, linktosend)

            #update the stats
            self.xmit_queue = []
            self.efficiency = []
            self.bandwidth_used = []
            self.rtt_estimate = []
            for bsnd in self.bsend:
                self.xmit_queue.append(bsnd.sendq_size())
                self.efficiency.append(bsnd.get_efficiency())
                self.bandwidth_used.append(bsnd.get_bandwidth_used())
                self.rtt_estimate.append(bsnd.get_rtt_estimate())

    def send_image(self, img, frame_time, priority, linktosend=None):
        '''send an image object to the GCS'''
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.camera_settings.qualitysend]
        (result, jpeg) = cv2.imencode('.jpg', img, encode_param)

        # keep filtered image size
        self.jpeg_size = 0.95 * self.jpeg_size + 0.05 * len(jpeg)

        pkt = cuav_command.ImagePacket(frame_time, jpeg, priority)
        self.transmit_queue.put((pkt, priority, linktosend))

    def start_aircraft_bsend(self):
        '''start bsend for aircraft side'''
        if len(self.bsend) == 0:
            for lnk in self.camera_settings.gcs_address.split(','):
                try:
                    [remoteip, remoteport, localport, bw] = lnk.split(':')
                    newbsnd = block_xmit.BlockSender(bandwidth=int(bw), debug=False,
                                        dest_ip=remoteip, dest_port=int(remoteport), port=int(localport))
                    self.bsend.append(newbsnd)
                except:
                    print("Bad GCS endpoint (must be remIP:remport:localport:bw): " + str(lnk))
                    pass

    def start_thread(self, fn):
        '''start a thread running'''
        t = threading.Thread(target=fn)
        t.daemon = True
        t.start()
        return t

    def unload(self):
        '''unload module'''
        self.running = False
        self.unload_event.set()
        if self.capture_thread is not None:
            self.capture_thread.join(1.0)
            self.scan_thread.join(1.0)
            self.transmit_thread.join(1.0)
        print('camera unload OK')

    def check_commands(self, bsend):
        '''check for remote commands'''
        if bsend is None:
            return
        buf = bsend.recv(0)
        if buf is None:
            return
        try:
            obj = cPickle.loads(str(buf))
            if obj == None:
                return
        except Exception as e:
            return

        if isinstance(obj, cuav_command.StampedCommand):
            if obj.timestamp in self.handled_timestamps:
                # we've seen this packet before, discard
                return
            self.handled_timestamps[obj.timestamp] = time.time()

        if isinstance(obj, cuav_command.ImageRequest):
            self.handle_image_request(obj, bsend)

        if isinstance(obj, cuav_command.ChangeCameraSetting):
            self.camera_settings.set(obj.name, obj.value)
            self.camera_settings_callback(obj)

        if isinstance(obj, cuav_command.ChangeImageSetting):
            self.image_settings.set(obj.name, obj.value)
            self.image_settings_callback(obj)

        if isinstance(obj, cuav_command.CommandPacket):
            self.cmd_camera([obj.command])

    def mavlink_packet(self, m):
        '''handle an incoming mavlink packet'''
        if self.mpstate.status.watch in ["camera","queue"] and time.time() > self.last_watch+1:
            self.last_watch = time.time()
            self.cmd_camera(["status" if self.mpstate.status.watch == "camera" else "queue"])
        # update position interpolator
        self.mpos.add_msg(m)
        if m.get_type() == 'SYSTEM_TIME' and self.camera_settings.clock_sync and self.capture_thread is not None:
            # optionally sync system clock on the capture side
            self.sync_gps_clock(m.time_unix_usec)
        if m.get_type() == 'VFR_HUD' and self.airstart_triggered and not self.running:
            #if the airstart is triggered and we're flying, then start capture
            if m.airspeed > self.camera_settings.minspeed or m.groundspeed > self.camera_settings.minspeed:
                self.running = True
                self.joelog = cuav_joe.JoeLog(os.path.join(os.path.dirname(self.camera_settings.imagefile), 'joe_air.log'), append=self.continue_mode)
                self.capture_thread = self.start_thread(self.capture_threadfunc)
                self.scan_thread = self.start_thread(self.scan_threadfunc)
                self.send_message("Started cuav running")
                print("Started cuav running")
        if m.get_type() == "TERRAIN_REPORT":
            self.terrain_alt = m.current_height

    def sync_gps_clock(self, time_usec):
        '''sync system clock with GPS time'''
        if time_usec == 0:
            # no GPS lock
            return
        if os.geteuid() != 0:
            # can only do this as root
            return
        time_seconds = time_usec*1.0e-6
        if self.have_set_gps_time and abs(time_seconds - time.time()) < 10:
            # only change a 2nd time if time is off by 10 seconds
            return
        t1 = time.time()
        cuav_util.set_system_clock(time_seconds)
        t2 = time.time()
        print("Changed system time by %.2f seconds" % (t2-t1))
        self.have_set_gps_time = True

    def handle_image_request(self, obj, bsend):
        '''handle ImageRequest from GCS. Only sends to the requesting GCS'''
        filename = self.imagefilenamemapping[str(obj.frame_time)]
        if not os.path.exists(filename):
            print("No file: %s" % filename)
            return
        try:
            img = cv2.imread(filename, -1)
        except Exception:
            return
        if not obj.fullres:
            im_small = cv2.resize(img, (0,0), fx=0.5, fy=0.5)
            img = im_small
        print("Sending image %s" % filename)
        self.send_image(img, obj.frame_time, 10000, bsend)

    def camera_settings_callback(self, setting):
        '''called on a changed camera setting'''
        pkt = cuav_command.ChangeCameraSetting(setting.name, setting.value)
        self.transmit_queue.put((pkt, None, None))

    def image_settings_callback(self, setting):
        '''called on a changed image setting'''
        pkt = cuav_command.ChangeImageSetting(setting.name, setting.value)
        self.transmit_queue.put((pkt, None, None))

    def send_heartbeat(self):
        '''send a heartbeat'''
        pkt = cuav_command.HeartBeat()
        self.transmit_queue.put((pkt, None, None))

    def send_message(self, msg):
        '''send a message'''
        pkt = cuav_command.CameraMessage(msg)
        self.transmit_queue.put((pkt, None, None))

    def send_object_complete(self, obj):
        '''called on complete of an send_object, cancelling send on other link
        Not used for now, as we're assuming some links are to different GCS'''
        pass
        #if obj.blockid is not None:
        #    for bsnd in self.bsend:
        #        bsnd.cancel(obj.blockid)

    def send_object(self, obj, priority=None, linktosend=None):
        '''send an object to all links if linktosend is none
        otherwise just send to the specified link'''
        buf = cPickle.dumps(obj, cPickle.HIGHEST_PROTOCOL)
        if priority is None:
            priority = 10000
        #only send if the queue is not clogged
        if not linktosend:
            for bsnd in self.bsend:
                if bsnd.sendq_size() < self.camera_settings.maxqueue:
                    obj.blockid = bsnd.send(buf, priority=priority, callback=functools.partial(self.send_object_complete, obj))
        else:
            if linktosend.sendq_size() < self.camera_settings.maxqueue:
                obj.blockid = linktosend.send(buf, priority=priority, callback=functools.partial(self.send_object_complete, obj))

def init(mpstate):
    '''initialise module'''
    return CameraAirModule(mpstate)
