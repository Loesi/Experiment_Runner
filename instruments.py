import propar, time, usb.core, struct, lmfit, serial, random
import numpy as np
from typing import Callable, Any

class ErrorInstrument:
    def __init__(self, name):
        self.name = name
    
    def status(self, i: int) -> tuple[str, bool]:
        return f'%2.2d.%8.8s: \033[41m  \033[0m xxxx' %(i, self.name), False


class CONTROLLER:
    def __init__(self, name, set_fc: Callable[[np.number]], read_fc: Callable[[], np.number | None], init_calc_set_value_fc: Callable[[np.number], np.number], default: np.number):
        """Controller object that is used to controll a specific parameter with the experiment runner"""
        self.name = name
        self._set_fc = set_fc
        self._read_fc = read_fc
        self._calc_set_value = init_calc_set_value_fc
        self._default = default

    def status(self, i: int) -> tuple[str, bool]:
        curr_val = self._read_fc()
        ok = curr_val != None
        if ok:
            s = '%2.2d.%8.8s: \033[42m  \033[0m %4.3g' %(i, self.name, curr_val)
        else:
            s = '%2.2d.%8.8s: \033[41m  \033[0m xxxx' %(i, self.name)
        return s, ok

    
    def set(self, x: np.number):
        """sets the controller to the requested Value"""
        self._set_fc(self._calc_set_value(x))
        
    def read_set(self):
        return self._read_fc()
    
    def set_default(self):
        self.set(self._default)

    # def calibrate(self, sensor_call: Callable[[None], np.number]):
    #     """calibrates the conversion between the request value and the controllers set value"""
    #     ys = np.linspace(0, self._max_set_value, 100)
    #     xs = []
    #     for i in ys:
    #         time.sleep(0.5)
    #         self._set_fc(i)
    #         time.sleep(0.5)
    #         ys.append(sensor_call)
    #     xs = np.array(xs)

    #     model = lmfit.models.LinearModel()
    #     res = model.fit(data = ys, x = xs)
    #     self._calc_set_value = lambda x: res.eval(x)
    

class SENSOR:
    def __init__(self, name, read_fc: Callable[[], np.number | None]):
        """Sensor object that can be read by the experiment runner"""
        self.name = name
        self._read_fc = read_fc

    def status(self, i: int) ->tuple[str, bool]:
        curr_val = self._read_fc()
        ok = (curr_val != np.nan) and (curr_val != None)
        if ok:
            s = '%2.2d.%8.8s: \033[42m  \033[0m %4.3g' %(i, self.name, curr_val)
        else:
            s = '%2.2d.%8.8s: \033[41m  \033[0m xxxx' %(i, self.name)
        return s, ok

    
    def read(self) -> np.number | None:
        """read the current value of the sensor"""
        try:
            return self._read_fc()
        except:
            return np.float64(np.nan)
        
class ALARM:
    def __init__(self, conf: str):
        conf_elements = conf.split()
        try:
            float(conf_elements[0])
        except ValueError:
            self.left: str | np.number = conf_elements[0]
        else:
            self.left = np.float64(conf_elements[0])
        try:
            float(conf_elements[2])
        except ValueError:
            self.right: str | np.number = conf_elements[2]
        else:
            self.right = np.float64(conf_elements[2])
        self._operator = conf_elements[1]
        match conf_elements[1]:
            case "<":
                self._check = lambda l,r: l < r
            case "<=":
                self._check = lambda l,r: l <= r
            case ">=":
                self._check = lambda l,r: l >= r
            case ">":
                self._check = lambda l,r: l > r
            case _:
                raise ValueError(f"Operator {conf_elements[1]} was not recognized. Valid operators: <, <=, >=, >")
    
    def check(self, SENSs: dict[str, float]) -> bool:
        left = self.left if isinstance(self.left, np.number) else SENSs[self.left]
        right = self.right if isinstance(self.right, np.number) else SENSs[self.right]
        return self._check(left, right)
    
    def test(self, SENSs: dict[str, SENSOR | ErrorInstrument]) -> bool:
        for v in [self.left, self.right]:
            if not isinstance(v, float) and v not in SENSs.keys():
                return False
        return True
    
    def print(self):
        print(f"active alarm: {self.left} {self._operator} {self.right}")


bronkhorst_devices: dict[str, propar.instrument | None] = {}
def get_bronkhorst_device(port: str, address: int) -> propar.instrument | None:
    key = f"{port}-{address}"
    if key not in bronkhorst_devices.keys():
        try:
            bronkhorst_devices[key] = propar.instrument(port, address=address)
        except serial.SerialException:
            bronkhorst_devices[key] = None
    return bronkhorst_devices[key]

class ThermocoupleArray:
    """Handles the physical communication and caching for the thermocouple array."""
    
    def __init__(self, readout_cutoff_s: float = 0.5):
        self.ENDPOINT_IN = 0x81
        self.ENDPOINT_OUT = 0x01
        self._channel_No: int = 4
        self._vid = 0x09DB
        self._pid = 0x0090

        self._last_read_time = 0.0
        self._readout_cutoff_s = readout_cutoff_s
        self._cached_data: list[Any] = [np.nan] * self._channel_No

        self._configure_Array()
    
    def _configure_Array(self):
        found_dev = usb.core.find(idVendor = self._vid, idProduct = self._pid)
        if isinstance(found_dev, usb.core.Device):
            self.dev = found_dev
            self.dev.set_configuration()
        else:
            self.dev = None

    def _handle_reconnection(self):
        try:
            if self.dev:
                usb.util.dispose_resources(self.dev)
        except:
            pass
        self.dev = None
        self._cached_data = [np.nan] * self._channel_No
        self._configure_Array()
        try:
            assert self.dev is usb.core.Device
            self.dev.read(self.ENDPOINT_IN, 33, timeout = 5)
        except:
            pass
            
    def _low_level_bulk_read(self) -> None:
        """Reads out the Temperatur Values from the Thermocouples and stores them in the cache"""
        temperatures = []
        try:
            assert self.dev is usb.core.Device
            self.dev.write(self.ENDPOINT_OUT, [0x19, 0x01, 0x05, 0x00], timeout = 500)
            data = self.dev.read(self.ENDPOINT_IN, 33, timeout = 500)
            
            if len(data) >= 32:
                for start, end in [(1, 5), (5, 9), (13, 17), (17, 21)]:
                    temperature_raw_bytearray = bytearray(data[start:end])
                    temperatures.append(struct.unpack('<f', temperature_raw_bytearray)[0])
                self._cached_data = temperatures
                self._last_read_time = time.time()
                return
            else:
                print("Insufficient data received. Resetting connection...")
                self._handle_reconnection()
                return 
                
        except (usb.core.USBError, Exception, AttributeError) as e:
            print(f"USB Error: {e}. Attempting recovery...")
            self._handle_reconnection()
            return 

    def readParameter(self, channel_id: int) -> np.number:
        """Retrieve Sensor data for specified channel"""
        if channel_id >= self._channel_No:
            print(f"channel_id: {channel_id}")
            raise ValueError(f"channel_id {channel_id} is out of the channel range. Only {self._channel_No} accessible.")
        
        if time.time() - self._last_read_time > self._readout_cutoff_s:
            self._low_level_bulk_read()
        
        return self._cached_data[channel_id]

class LinearInterpolator:
    def __init__(self, duration):
        rng = np.random.default_rng()
        self.duration: float = duration * 60
        self.start_time: float|None = None
        self.start_val: np.int_ = rng.integers(0,100)
        self.end_val: np.int_ = rng.integers(0,100)
        self.end_time: float|None = None

    def __call__(self) -> np.number:
        if isinstance(self.start_time, type(None)):
            self.start_time = time.time()
            self.end_time = self.start_time + self.duration

        assert isinstance(self.start_time, float)
        assert isinstance(self.end_time, float)

        current_time = time.time()
        if current_time > self.end_time:
            return self.end_val
        else:
            progress = (current_time - self.start_time) / self.duration
            return self.start_val * (1-progress) + self.end_val * progress
        

thermocoupleArray: ThermocoupleArray|None = None
def init_CONTROLLER(device_type: str, *conf) -> tuple[str, CONTROLLER | ErrorInstrument]:
    match device_type:
        case "bronkhorst":
            name, port, channel, DDE, fluid_set_idx = conf[:5]

            device = get_bronkhorst_device(port, 128)
            if isinstance(device, type(None)):
                return name, ErrorInstrument(name=name)

            device.writeParameter(24, int(fluid_set_idx))
            set_fc: Callable[[np.number]] = lambda x: device.writeParameter(int(DDE), x, channel=int(channel))
            read_fc: Callable[[], np.number| None] = lambda: device.readParameter(int(DDE), channel=int(channel))
            return name, CONTROLLER(
                name = name,
                set_fc = set_fc,
                read_fc = read_fc,
                init_calc_set_value_fc = lambda x: x,
                default = np.int8(0),
            )
        
        case "bronkhorst_legacy":
            name, port, channel, address, DDE = conf[:5]

            device = get_bronkhorst_device(port, int(address))
            if isinstance(device, type(None)):
                return name, ErrorInstrument(name=name)

            set_fc: Callable[[np.number]] = lambda x: device.writeParameter(int(DDE), x, channel=int(channel))
            read_fc: Callable[[], np.number | None] = lambda: device.readParameter(int(DDE), channel=int(channel))
            return name, CONTROLLER(
                name=name,
                set_fc=set_fc,
                read_fc=read_fc,
                init_calc_set_value_fc= lambda x: x,
                default=np.int8(0),
            )
            
        case "dummy":
            name, = conf[:1]
            return name, CONTROLLER(
                name = name,
                set_fc = lambda x: print(f"Controller {name:>4s} set to {x}."),
                read_fc = lambda: np.int8(0),
                init_calc_set_value_fc = lambda x: x,
                default = np.int8(0)
            )

        case _:
            raise InstrumentConfigError(device_type, "CONT")
        
def init_SENSOR(device_type: str, *conf) -> tuple[str, SENSOR | ErrorInstrument]:
    match device_type:
        case "bronkhorst":
            name, port, channel, DDE = conf[:4]

            device = get_bronkhorst_device(port, 128)
            if isinstance(device, type(None)):
                return name, ErrorInstrument(name=name)

            return name, SENSOR(
                name = name,
                read_fc = lambda: device.readParameter(int(DDE), channel=int(channel))
                )
        
        case "bronkhorst_legacy":
            name, port, channel, address, DDE = conf[:5]

            device = get_bronkhorst_device(port, int(address))
            if isinstance(device, type(None)):
                return name, ErrorInstrument(name=name)

            return name, SENSOR(
                name = name,
                read_fc = lambda: device.readParameter(int(DDE), channel=int(channel))
                )
            
        case "thermocouple":
            name, channel = conf[:2]
            global thermocoupleArray
            if isinstance(thermocoupleArray, type(None)):
                thermocoupleArray = ThermocoupleArray()

            try:
                thermocoupleArray.readParameter(int(channel))
            except:
                return name, ErrorInstrument(name=name)
            
            assert thermocoupleArray is not None
            return name, SENSOR(
                name = name,
                read_fc = lambda: thermocoupleArray.readParameter(int(channel))
            )
            
        case "dummy":
            name, duration = conf[:2]

            return name, SENSOR(
                name = name,
                read_fc = LinearInterpolator(duration=int(duration))
            )
        
        case _:
            raise InstrumentConfigError(device_type, "SENS")
        

    
class InstrumentConfigError(ValueError):
    """
    Custom exception raised when an instrument_type is configured 
    with an invalid device_type combination.
    """
    def __init__(self, device_type: str, instrument_type: str, message: str = ""):
        super().__init__(f"Invalid configuration combo: Device Type '{device_type}' does not support Instrument Type '{instrument_type}'. {message}")