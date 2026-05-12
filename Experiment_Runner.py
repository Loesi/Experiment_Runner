# -*- coding: utf-8 -*-
"""
Version 1.1
December 2025
Author: JLöseke
"""

import argparse, csv, io, math, os, playsound, queue, signal, sys, threading, time
from matplotlib import pyplot, lines
from pathlib import Path
from typing import Callable, Tuple, Dict, List

from instruments import *

PROCEDURE_DIR_NAME = ".procedure_files"

def read_file(file: os.PathLike) -> list[list[str]]:
    elements = []

    with open(file, 'r') as f:
        for line in f:
            content = line.split('#', 1)[0].strip()
            if content.isspace() or len(content) == 0:
                continue
            else:
                elements.append([s.strip() for s in content.split(",")])

    return elements

def is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except:
        return False


runner_instance = None
def handle_sigint(sig, frame):
    if runner_instance:
        runner_instance.shutdown()


def graceful_exit(exit_fc: Callable[[None], None]):
    def decorator(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            finally:
                exit_fc(self)
        return wrapper
    return decorator

def shutdown(instance):
    print("Gracefully shutting down!")
    instance.shutdown()

class Experiment_Controller:

    def __init__(self, conf_name: os.PathLike|None = None, save: bool = True):
        self.print_header()
        self.save: bool = save

        self.controllers: Dict[str, CONTROLLER|ErrorInstrument] = {}
        self.sensors: Dict[str, SENSOR|ErrorInstrument] = {}
        self.alarms: List[ALARM] = []
        self.sens_display: dict[str, int] = {}
        for conf in self._read_config(conf_name):
            self._init_instrument(*conf)
        if not all(a.test(self.sensors) for a in self.alarms):
            raise ValueError("Error in Alarm configuration: See log for more details.")

        self._print_device_status()

    def print_header(self):
        print(
            r"""
            ___________                           .__                       __   
            \_   _____/__  _________   ___________|__| _____   ____   _____/  |_ 
             |    __)_\  \/  /\____ \_/ __ \_  __ \  |/     \_/ __ \ /    \   __\
             |        \>    < |  |_> >  ___/|  | \/  |  Y Y  \  ___/|   |  \  |  
            /_______  /__/\_ \|   __/ \___  >__|  |__|__|_|  /\___  >___|  /__|  
                    \/      \/|__|        \/               \/     \/     \/      
                       __________                                                
                       \______   \__ __  ____   ____   ___________               
                        |       _/  |  \/    \ /    \_/ __ \_  __ \              
                        |    |   \  |  /   |  \   |  \  ___/|  | \/              
                        |____|_  /____/|___|  /___|  /\___  >__|                 
                               \/           \/     \/     \/                     

            """
        )

    def _read_config(self, conf: os.PathLike|None) -> List[List[str]]:
        script_dir = Path(__file__).resolve().parent
        if isinstance(conf, type(None)):
            return read_file(script_dir / 'config.txt')
        else:
            return read_file(script_dir / conf)

    def _init_instrument(self, instrument_type: str, *conf: str) -> None:
        match instrument_type[:4]:
            case "CONT":
                name, CONT = init_CONTROLLER(*conf)
                if name in self.controllers.keys():
                    raise ValueError(f"Multiple CONTROLLERs with the name {name} defined")
                else:
                    self.controllers[name] = CONT

            case "SENS":
                name, SENS = init_SENSOR(*conf)
                if name in self.sensors.keys():
                    raise ValueError(f"Multiple SENSORs with the name {name} defined")
                else:
                    self.sensors[name] = SENS
                
                try:
                    self.sens_display[name] = int(instrument_type[4:])-1
                except:
                    pass
            
            case "WARN":
                self.alarms.append(ALARM(conf[0]))

            case _:
                raise ValueError(f"instrument_type has to be 'CONT' or 'SENS' and not {instrument_type}")

    def _print_device_status(self):
        """Generates and prints the multi-column status display."""
        all_ok = True
        n_min_cols = 5
        controllers = list(self.controllers.keys())
        sensors = list(self.sensors.keys())

        c_cols = 2 if len(controllers) > n_min_cols and (len(controllers) > 2*len(self.sensors.keys())) else 1
        s_cols = 2 if len(sensors) > n_min_cols and (len(sensors)> 2*len(controllers)) else 1
        rows = math.ceil(max(len(self.controllers.keys()) / c_cols, len(self.sensors.keys()) / s_cols))

        print((" %-20s " % "Controllers:") + " " * 22 + (" %-20s " % "Sensors:"))

        strings = [""] * rows
        for i in range(rows*2):
            if i < len (controllers):
                status, ok = self.controllers[controllers[i]].status(i)
                if not ok: all_ok = False
                strings[i%rows] += " " + status + " "
            else:
                strings[i%rows] += " " * 22

        for i in range(rows*2):
            if i < len (sensors):
                status, ok = self.sensors[sensors[i]].status(i)
                if not ok: all_ok = False
                strings[i%rows] += " " + status + " "
            else:
                strings[i%rows] += " " * 22

        for l in strings:
            print(l)
        print(" " + "_"*(4*22-2) + " ")
        print("")

        self.ready = all_ok

    @graceful_exit(shutdown)
    def run(self, sample: str, procedure_file: str, comment: str = ""):
        global runner_instance
        runner_instance = self
        
        if not self.ready:
            print("Some devices experience issues")
            return

        if procedure_file[-4:] != ".txt":
            procedure_file += ".txt"
        proc_file = Path.home() / PROCEDURE_DIR_NAME / procedure_file
        if proc_file.exists():
            self.setpoints = read_file(proc_file)
        if Path(procedure_file).exists():
            self.setpoints = read_file(Path(procedure_file))
        else:
            print(f"No procedure file named '{procedure_file}' found. The file has to exist in {os.path.join(Path.home(), PROCEDURE_DIR_NAME, procedure_file)} or the relative path has to be provided.")
            return

        if not self.check_setpoints():
            return

        self.logfile = self.save_name(sample, os.path.splitext(os.path.basename(procedure_file))[0])
        self.comment = comment
        self.data_queue = queue.Queue()
        self.data_lock = threading.Lock()
        self.alarm_lock = threading.Lock()
        self.alarm_active = False
        self.active_alarms = []
        self.timestamps: List[float] = []
        self.readouts: Dict[str, List[float]] = {s: [] for s in self.sensors}

        pyplot.ion()
        ax_number = max(self.sens_display.values())+1
        self.fig, self.axes = pyplot.subplots(ax_number, 1, sharex="all")
        plot_lines: Dict[str, lines.Line2D] = {}

        for sens_name, ax_idx in self.sens_display.items():
            plot_lines[sens_name] = self.axes[ax_idx].plot([], [], label=sens_name)[0]
        for ax in self.axes:
            ax.legend(frameon=False)
        self.axes[-1].set_xlabel("Time (s)")

        self.threads = []
        self.threads.append(threading.Thread(target=self._file_writer_thread, name="WriterThread"))
        self.threads.append(threading.Thread(target=self._sensor_acquisition_thread, name="SensorThread"))
        self.threads.append(threading.Thread(target=self._controller_thread, name="ControlThread"))
        self.threads.append(threading.Thread(target=self._alarm_handling_thread, name="AlarmThread"))
        #self.threads.append(threading.Thread(target=self._plotting_thread, name="PlottingThread", args = [plot_lines]))

        self.running = True
        self.start:float = time.time()
        for t in self.threads:
            t.daemon = True
            t.start()

        self._plotting_thread(plot_lines)

        # control_thread = next(t for t in self.threads if t.name == "ControlThread")
        # control_thread.join()
        # self.running = False 

        for t in self.threads:
            if t.is_alive():
                t.join()

    def check_setpoints(self) -> bool:
        ok = True
        for i, setpoint in enumerate(self.setpoints):
            if len(setpoint) != len(self.controllers) + 1:
                print(f"Invalid setpoint (#{i}): Expected {len(self.controllers) + 1} values, recieved {len(setpoint)}")
                ok = False
            
            for s in setpoint[1:]:
                try:
                    float(s)
                except ValueError as e:
                    print(f"Invalid formatting of setpoint {i}: {e}")
                    ok = False

        return ok

    
    def save_name(self, sample, procedure_name) -> str:
        return "_".join([procedure_name, sample, time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(time.time()))+".txt"])
    
    def _sensor_acquisition_thread(self):
        """continuously readouts the sensors"""
        while self.running:
            cur_time = time.time()
            current_readings: Dict[str, np.number | None] = {'time': np.float128(cur_time-self.start)}

            for name, sensor in self.sensors.items():
                assert isinstance(sensor, SENSOR)
                current_readings[name] = sensor.read()
            self.data_queue.put(current_readings)

            time_to_wait = 1.0 - (time.time() - cur_time)
            if time_to_wait > 0:
                time.sleep(time_to_wait)
            else:
                print(f"Data aquisition at {time.strftime("%Y-%m-%d_%H:%M:%S", time.localtime(cur_time))} took longer longer than 1 second!")

        else:
            self.data_queue.put(None)
            print("\n--- Sensor Reader Shutdown ---")

    def _file_writer_thread(self):
        """writing the new readouts to the file as soon as they are avalible"""
        if self.save:
            with open(self.logfile, "a", newline="") as f:
                if self.comment != "":
                    f.write("# " + self.comment + "\n")
                writer = csv.DictWriter(f, fieldnames = ["time"] + list(self.sensors.keys()))
                writer.writeheader()

                self._write_loop(f, writer)
        else:
            self._write_loop()
            
            print("\n--- File writer Finished ---")

    def _write_loop(self, f:io.TextIOWrapper | None = None, writer: csv.DictWriter | None = None):
        doWrite = not (isinstance(writer, type(None)) or isinstance(f, type(None)))
        while self.running:
            data: Dict[str, float] | None = self.data_queue.get()
            if data is None:
                # exit condition
                self.alarm_active = False
                break 

            triggered_alarms=[a for a in self.alarms if a.check(data)]
            with self.alarm_lock:
                if triggered_alarms:
                    self.alarm_active = True
                    self.active_alarms = triggered_alarms
                else:
                    if self.alarm_active:
                        print("✅ Alarm condition cleared.")
                        self.alarm_active = False
                        self.active_alarms = []
                        
            try:
                if doWrite: writer.writerow(data)

                with self.data_lock:
                    self.timestamps.append(data["time"])
                    for name in self.sensors.keys():
                        self.readouts[name].append(data[name])
            
            except Exception as e:
                print("Error writing data to file")
            
            if doWrite: f.flush()
            self.data_queue.task_done()

            

    def _controller_thread(self):
        """Thread to controll the controllers to the setpoints"""
        setpoint_idx = 0
        step_end_time = self.start

        while self.running and setpoint_idx < len(self.setpoints):
            sp = self.setpoints[setpoint_idx]

            for i, (name, cont) in enumerate(self.controllers.items()):
                assert isinstance(cont, CONTROLLER)
                cont.set(np.float64(sp[1+i]))

            if sp[0].upper() == "C":
                c_names = self.controllers.keys()
                print("Current Controller Values are: " + ", ".join([f"{a} ({n})" for a,n in zip(sp[1:],c_names)]))
                input("Press Enter to switch to: " + ", ".join([f"{a} ({n})" for a,n in zip(self.setpoints[setpoint_idx+1][1:],c_names)]))
                step_end_time = time.time()
            elif is_numeric(sp[0]):
                step_end_time += float(sp[0]) * 60
                while self.running and time.time() < step_end_time - 1:
                    time.sleep(1)

                if not self.running:
                    break
                else:
                    time.sleep(step_end_time - time.time())
            else:
                raise ValueError(f"Value '{sp[0]}' could not be interpreted as a valid step length: expected 'C', 'inf' or a number")
            
            setpoint_idx += 1
        
        # main stopping routinge otherwise done via interrupts
        self.running = False
        print("\n--- Control Sequence Complete ---")

    def _plotting_thread(self, plot_lines: Dict[str, lines.Line2D], update_interval=1):
        """Thread for continuously displaying the data in a plt plot"""
        last_data_count = 0            
        cur_time = 0.1
        update_time = time.time()
        while self.running:
            data_updated = False
            with self.data_lock:
                if len(self.timestamps) > last_data_count:
                    cur_time = self.timestamps[-1]
                    for name, l in plot_lines.items():
                        l.set_data(self.timestamps, self.readouts[name])

                    last_data_count = len(self.timestamps)
                    data_updated = True
                    update_time = time.time()
            
            if data_updated: 
                for ax in self.axes:
                    # ax.set_xlim(0,cur_time)
                    # ax.set_ylim(0)
                    ax.relim()
                    ax.autoscale_view()
                self.fig.canvas.draw()
                self.fig.canvas.flush_events()

                sleep_time = (update_time + update_interval) - time.time()
                if sleep_time > 0:
                    time.sleep(sleep_time)

            else:
                time.sleep(update_interval/2)

        pyplot.ioff()
        pyplot.show(block=False)
    
    def _alarm_handling_thread(self):
        ALARM_SOUND_FILE = "ALARM.mp3"
        is_active = False

        while self.running:
            with self.alarm_lock:
                is_active = self.alarm_active
                
            if is_active:
                with self.alarm_lock:
                    print("\n🚨🚨 ALARM TRIGGERED 🚨🚨")
                    for a in self.active_alarms: a.print()
                try:
                    playsound.playsound(ALARM_SOUND_FILE)
                except Exception as e:
                    print(f"Could not play alarm sound: {e}")
                time.sleep(1)
                
            else:
                time.sleep(1) 
        print("\n--- Alarm Handler Finished ---")

    def shutdown(self):
        if not self.ready:
            return
        if hasattr(self, "running") and self.running:
            print("run aborted")
            self.running = False
        for _, cont in self.controllers.items():
            assert isinstance(cont, CONTROLLER)
            cont.set_default()

def parse_args():
    parser = argparse.ArgumentParser(
        description="A program for running experiments and diagnostics.",
        formatter_class=argparse.RawTextHelpFormatter 
    )
    parser.add_argument('sample', nargs='?', type=str, help='The required unique identifier for the current sample being processed.')
    parser.add_argument('prod_file', nargs='?', type=str, help='procedure file name (typically .txt)')

    parser.add_argument('-test', action='store_true', help='Run test_config and test_exp.')
    parser.add_argument('-check', action='store_true', help='Perform a system readiness check and exit.')
    parser.add_argument('-nosave', action='store_true', help='Will prevent the Programm from saving the readout.')
    parser.add_argument('-comment', type=str, default="", help='Optional comment that will be written at the top of the log file')
    parser.add_argument('-config', type=str, default="config.txt", help='Filepath to config file to use instead of \'config\'.')

    return parser.parse_args()

if __name__ == "__main__":
    # signal.signal(signal.SIGINT, handle_sigint)
    args = parse_args()
    
    if not os.path.exists(os.path.join(Path.home(), PROCEDURE_DIR_NAME)):
        os.mkdir(os.path.join(Path.home(), PROCEDURE_DIR_NAME))
    
    if args.test:
        print("--- Running Test Pathway ---")
        exp = Experiment_Controller(conf_name=Path("test_config.txt"), save= not args.nosave)
        exp.run("test","test_exp.txt")
        exit(0)
        
    elif args.check:
        print("--- Running Check Pathway ---")
        exp = Experiment_Controller(conf_name=args.config)
        exit(0)

    print("--- Running Main Experiment Pathway ---")
    exp = Experiment_Controller(conf_name=args.config, save= not args.nosave)
    exp.run(args.sample, args.prod_file, args.comment)