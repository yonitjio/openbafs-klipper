import logging, logging.handlers, threading, queue
import os.path
from datetime import datetime

# Forward all messages through a queue (polled by background thread)
class QueueHandler(logging.Handler):
    def __init__(self, queue):
        logging.Handler.__init__(self)
        self.queue = queue

    def emit(self, record):
        try:
            self.format(record)
            record.msg = record.message
            record.args = None
            record.exc_info = None
            self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)

# Poll log queue on background thread and log each message to logfile
class QueueListener(logging.handlers.TimedRotatingFileHandler):
    def __init__(self, filename):
        logging.handlers.TimedRotatingFileHandler.__init__(
            self, filename, when='midnight', backupCount=5)
        self.bg_queue = queue.Queue()
        self.bg_thread = threading.Thread(target=self._bg_thread)
        self.bg_thread.start()

    def _bg_thread(self):
        while True:
            record = self.bg_queue.get(True)
            if record is None:
                break
            self.handle(record)

    def stop(self):
        self.bg_queue.put_nowait(None)
        self.bg_thread.join()

# Class to improve formatting of multi-line messages
class MultiLineFormatter(logging.Formatter):
    def format(self, record):
        indent = ' ' * 9
        lines = super(MultiLineFormatter, self).format(record)
        return lines.replace('\n', '\n' + indent)

# Bafsd exception error class
class BafsdError(Exception):
    pass

class Bafsd:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = None

        self.port = -1
        self.last_port = -1

        self.extruder_name = config.get('extruder', 'extruder')
        self.servo_names = list(config.getlist('servos', ['bafsd_servo_00',
                                                          'bafsd_servo_01',
                                                          'bafsd_servo_02',
                                                          'bafsd_servo_03']))
        self.fil_sensor_name = config.get('filament_sensor', 'bafsd_fil_sensor')
        self.stepper_name = config.get('stepper', 'bafsd_stepper')
        self.retract_distance = list(config.getintlist('retract_distance', [95, 95, 95, 95]))
        self.feed_distance = list(config.getintlist('feed_distance', [98, 98, 98, 98]))
        self.small_feed_distance = list(config.getintlist('small_feed_distance', [2, 2, 2, 2]))
        self.slower_margin = list(config.getintlist('slower_margin', [10, 10, 10, 10]))
        self.unload_speed = config.getint('unload_speed', 20)
        self.stepper_speed = config.getint('stepper_speed', 50)
        self.stepper_slower_speed = config.getint('stepper_slower_speed', 5)
        self.stepper_accel = config.getint('stepper_accel', 50)
        self.servo_on_deg = list(config.getintlist('servo_on_deg', [29, 157, 29, 157]))
        self.servo_off_deg = list(config.getintlist('servo_off_deg', [90, 90, 90, 90]))
        self.sensor_to_gear_distance = config.getint('sensor_to_gear_distance', 35)
        self.sensor_to_gear_margin = config.getint('sensor_to_gear_margin', 5)
        self.filament_catching_margin =config.getint('filament_catching_margin', 2)

        self.fil_sensor = self.printer.lookup_object("state_button %s" % self.fil_sensor_name, None)
        self.servos = [self.printer.lookup_object("servo %s" % self.servo_names[0], None),
            self.printer.lookup_object("servo %s" % self.servo_names[1], None),
            self.printer.lookup_object("servo %s" % self.servo_names[2], None),
            self.printer.lookup_object("servo %s" % self.servo_names[3], None)]

        self.extruder = self.printer.lookup_object(self.extruder_name, None)
        self.stepper = self.printer.lookup_object("manual_stepper %s" % self.stepper_name, None)

        self.gcode.register_command('FS_STATUS', self.cmd_BAFSD_STATUS, desc = self.cmd_BAFSD_STATUS_help)
        self.gcode.register_command('FS_RESET', self.cmd_BAFSD_RESET, desc = self.cmd_BAFSD_RESET_help)
        self.gcode.register_command('FS_SELECT', self.cmd_BAFSD_SELECT, desc = self.cmd_BAFSD_SELECT_help)
        self.gcode.register_command('FS_SMALL_FEED', self.cmd_BAFSD_SMALL_FEED, desc = self.cmd_BAFSD_SMALL_FEED_help)
        self.gcode.register_command('FS_FEED', self.cmd_BAFSD_FEED, desc = self.cmd_BAFSD_FEED_help)
        self.gcode.register_command('FS_MOVE_STEPPER', self.cmd_BAFSD_MOVE_STEPPER, desc = self.cmd_BAFSD_MOVE_STEPPER_help)
        self.gcode.register_command('FS_FIL_SENSOR', self.cmd_BAFSD_FS_FIL_SENSOR, desc = self.cmd_BAFSD_FS_FIL_SENSOR_help)
        self.gcode.register_command('FS_SWITCH', self.cmd_BAFSD_SWITCH, desc = self.cmd_BAFSD_SWITCH_help)

        self.printer.register_event_handler('klippy:connect', self.handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self.handle_disconnect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

    def _setup_logging(self):
        # Setup background file based logging before logging any messages
        logfile_path = self.printer.start_args['log_file']
        dirname = os.path.dirname(logfile_path)
        if dirname is None:
            log = '/tmp/bafsd.log'
        else:
            log = dirname + '/bafsd.log'
        self.queue_listener = QueueListener(log)
        self.queue_listener.setFormatter(MultiLineFormatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
        queue_handler = QueueHandler(self.queue_listener.bg_queue)
        self.logger = logging.getLogger('bafsd')
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(queue_handler)

    cmd_BAFSD_STATUS_help = "Status"
    def cmd_BAFSD_STATUS(self, gcmd):
        gcmd.respond_info("STATUS:"
                          + "\n  Port: %d" % self.port
                          + "\n  Last Port: %d" % self.last_port
                          + "\n  Filament Switch: %s" % self.get_filament_status()
                          )

    cmd_BAFSD_RESET_help = "Reset"
    def cmd_BAFSD_RESET(self, gcmd):
        self.reset()

    cmd_BAFSD_SELECT_help = "Select port"
    def cmd_BAFSD_SELECT(self, gcmd):
        port = gcmd.get_int('PORT', 0, minval=0, maxval=3)
        servo = gcmd.get_int('SERVO', 0, minval=0, maxval=1)
        self.select_port(port, servo)

        gcmd.respond_info("Port selected: %d" % port)

    cmd_BAFSD_SMALL_FEED_help = "Feed small amount of filament"
    def cmd_BAFSD_SMALL_FEED(self, gcmd):
        dist, speed, accel = self.small_feed()
        gcmd.respond_info("Feeding: %d at %d with %d" % (dist, speed, accel))

    cmd_BAFSD_FEED_help = "Feed filament"
    def cmd_BAFSD_FEED(self, gcmd):
        dist = gcmd.get_float('DIST', None)
        dist, speed, accel = self.feed(dist)
        gcmd.respond_info("Feeding: %d at %d with %d" % (dist, speed, accel))

    cmd_BAFSD_MOVE_STEPPER_help = "Feed small amount of filament"
    def cmd_BAFSD_MOVE_STEPPER(self, gcmd):
        dist = gcmd.get_int('DIST', 0)
        slow = gcmd.get_int('SLOW', 0, minval=0, maxval=1)
        self.enable_stepper()
        speed, accel = self.move_stepper(dist, slow)
        self.disable_stepper()
        gcmd.respond_info("moving: %d at %d with %d" % (dist, speed, accel))

    cmd_BAFSD_FS_FIL_SENSOR_help = "Get filament sensor state"
    def cmd_BAFSD_FS_FIL_SENSOR(self, gcmd):
        if self.fil_sensor is not None:
            gcmd.respond_info("Filament Sensor: " + self.get_filament_status())
        else:
            gcmd.respond_info("Filament Sensor: NOT SET")

    cmd_BAFSD_SWITCH_help = "Switch filament to selected port"
    def cmd_BAFSD_SWITCH(self, gcmd):
        port = gcmd.get_int('PORT', 0, minval=0, maxval=3)
        self.switch_port(port)
        gcmd.respond_info("Switched to port: %d" % port)

    def reset(self):
        self.port = -1
        self.last_port = -1

    def _set_port(self, port):
        self.last_port = self.port
        self.port = port

    def select_port(self, port, turn_on_servo):
        self._set_port(port)
        if turn_on_servo:
            self.activate_servo(self.port)
        else:
            self.deactivate_servos()

    def disable_stepper(self):
        self.stepper.do_enable(False)

    def enable_stepper(self):
        self.stepper.do_enable(True)

    def move_stepper(self, dist, slow=True, sync=False):
        self.stepper.do_set_position(0.)

        speed = self.stepper_slower_speed if slow else self.stepper_speed
        accel = self.stepper_accel
        self.stepper.do_move(dist, speed, accel, sync)
        return speed, accel

    def small_feed(self):
        if self.port == -1:
            return 0, 0, 0

        self.activate_servo(self.port)

        dist = self.get_small_feed_distance(self.port)
        self.enable_stepper()
        speed, accel = self.move_stepper(dist)
        self.disable_stepper()

        self.set_servo_off(self.port)
        return dist, speed, accel

    def feed(self, dist=None):
        if self.port == -1:
            return 0, 0, 0

        self.activate_servo(self.port)

        if dist is None:
            dist = self.get_feed_distance(self.port)

        self.enable_stepper()
        speed, accel = self.move_stepper(dist, slow=False)
        self.disable_stepper()

        self.set_servo_off(self.port)
        return dist, speed, accel

    def get_filament_status(self):
        state = self.fil_sensor.get_status()['state']
        if state == "PRESSED":
            return "PRESENT"
        return "ABSENT"

    def switch_port(self, port):
        def load_to_sensor():
            dist = self.sensor_to_gear_distance - self.sensor_to_gear_margin
            pos = self.toolhead.get_position()
            pos[3] += dist
            self.toolhead.manual_move(pos, speed)
            self.toolhead.dwell(0.05)
            self.toolhead.wait_moves()

            f_status = self.get_filament_status()
            while (f_status == "ABSENT") and (dist > 0):
                dist = dist - 1
                pos = self.toolhead.get_position()
                pos[3] += 1
                self.toolhead.manual_move(pos, speed)
                self.toolhead.dwell(0.05)
                self.toolhead.wait_moves()
                f_status = self.get_filament_status()

        def __slow_feed(p):
            self._log("Slow feed start: " + datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
            dist = self.get_slower_margin(p) + (5 if (p % 2) == 0 else -5)
            self.move_stepper(dist, slow=True, sync=False)

        def __move_extruder(d, s):
            self._log("Move extruder start: " + datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
            pos = self.toolhead.get_position()
            pos[3] += d
            self.toolhead.manual_move(pos, s)

        if self.port == -1:
            self.port = port
            return

        if self.port == port:
            return

        speed = self.unload_speed

        self._log("Unloading to sensor")
        fil_status = self.get_filament_status()
        self._log("Filament: %s" % fil_status)
        while fil_status == "PRESENT":
            pos = self.toolhead.get_position()
            pos[3] -= 1
            self.toolhead.manual_move(pos, speed)
            self.toolhead.dwell(0.1)
            self.toolhead.wait_moves()
            fil_status = self.get_filament_status()

        self._log("Filament: %s" % fil_status)
        self._log("Unloading to gear")
        pos = self.toolhead.get_position()
        pos[3] -= self.sensor_to_gear_distance
        self.toolhead.manual_move(pos, speed)
        self.toolhead.dwell(0.1)
        self.toolhead.wait_moves()

        self._log("Switching filament")
        self.enable_stepper()
        old_port = self.port
        new_port = port
        # retract
        self._log("Retracting")
        self.activate_servo(old_port)
        dist = self.get_retract_distance(old_port)
        self.move_stepper(dist, slow=False)
        self.set_servo_off(old_port)

        self.toolhead.dwell(0.1)
        self.toolhead.wait_moves()

        # feed
        self._log("Feeding")
        self.activate_servo(new_port)
        dist = self.get_feed_distance(new_port) - self.get_slower_margin(new_port)
        self.move_stepper(dist, slow=False)
        self.toolhead.dwell(0.1)
        self.toolhead.wait_moves()

        self._log("Slow feeding")
        slow_dist = abs(self.get_slower_margin(new_port)) + self.filament_catching_margin
        slow_speed = self.stepper_slower_speed

        #sf_thread = threading.Thread(target=__slow_feed, args=(new_port,))
        # x_thread = threading.Thread(target=__move_extruder, args=(slow_dist, slow_speed))

        #sf_thread.start()
        # x_thread.start()
        __slow_feed(new_port)
        __move_extruder(slow_dist, slow_speed)

        #sf_thread.join()
        # x_thread.join()

        self.set_servo_off(new_port)
        self.disable_stepper()

        self.toolhead.dwell(0.1)
        self.toolhead.wait_moves()

        self._log("Loading to sensor")
        load_to_sensor()

        fil_status = self.get_filament_status()
        self._log("Filament: %s" % fil_status)

        if fil_status == "ABSENT":
            self._log("Retrying")
            self.small_feed()
            self.toolhead.dwell(0.1)
            self.toolhead.wait_moves()
            load_to_sensor()
            fil_status = self.get_filament_status()


        if fil_status == "ABSENT":
            self._log("Failed to load to sensor")

        self._set_port(port)


    def handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self._setup_logging()

    def handle_disconnect(self):
        pass

    def handle_ready(self):
        pass

    def _log(self, message):
        self.logger.info(message)
        self.gcode.respond_info(message)

    def set_servo_on(self, port):
        self.servos[port].set_servo(angle=self.servo_on_deg[port])

    def set_servo_off(self, port):
        self.servos[port].set_servo(angle=self.servo_off_deg[port])

    def activate_servo(self, port):
        for i, s in enumerate(self.servos):
            if i == port:
                continue
            s.set_servo(angle=self.servo_off_deg[i])

        self.set_servo_on(port)

        self.toolhead.dwell(1)
        self.toolhead.wait_moves()


    def deactivate_servos(self):
        for i, s in enumerate(self.servos):
            s.set_servo(angle=self.servo_off_deg[i])

        self.toolhead.dwell(1)
        self.toolhead.wait_moves()

    def get_retract_distance(self, port):
        return (0 - self.get_adjusted_value(port, self.retract_distance[port]))

    def get_feed_distance(self, port):
        return self.get_adjusted_value(port, self.feed_distance[port])

    def get_small_feed_distance(self, port):
        return self.get_adjusted_value(port, self.small_feed_distance[port])

    def get_slower_margin(self, port):
        return self.get_adjusted_value(port, self.slower_margin[port])

    def get_adjusted_value(self, port, value):
        if (port % 2) == 0:
            return value
        else:
            return (0-value)

def load_config(config):
    return Bafsd(config)
