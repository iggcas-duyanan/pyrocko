import trace, util

import time, sys, logging, weakref
import numpy as num
from scipy import stats

logger = logging.getLogger('pyrocko.serial_hamster')

class QueueIsEmpty(Exception):
    pass

class Queue:
    def __init__(self, nmax):
        self.nmax = nmax
        self.queue = []
        
    def push_back(self, val):
        self.queue.append(val)
        while len(self.queue) > self.nmax:
            self.queue.pop(0)
        
    def mean(self):
        if not self.queue:
            raise QueueIsEmpty()
        return sum(self.queue)/float(len(self.queue))
    
    def median(self):
        if not self.queue:
            raise QueueIsEmpty()
        n = len(self.queue)
        s = sorted(self.queue)
        if n%2 != 0:
            return s[n/2]
        else:
            return (s[n/2-1]+s[n/2])/2.0
    
    def add(self, w):
        self.queue = [ v+w for v in self.queue ]
            
    def empty(self):
        self.queue[:] = []
        
class SerialHamsterError(Exception):
    pass

class SerialHamster:
    
    def __init__(self, port=0, baudrate=9600, timeout=5, buffersize=128,
                       network='', station='TEST', location='', channel='Z',
                       disallow_uneven_sampling_rates=True, 
                       deltat=None,
                       deltat_tolerance=0.01,
                       in_file=None):
        
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.buffersize = buffersize
        self.ser = None
        self.values = []
        self.times = []
        self.deltat = None
        self.deltat_tolerance = deltat_tolerance
        self.tmin = None
        self.previous_deltats = Queue(nmax=5)
        self.previous_tmin_offsets = Queue(nmax=5)
        self.ncontinuous = 0
        self.disallow_uneven_sampling_rates = disallow_uneven_sampling_rates
        self.network = network
        self.station = station
        self.location = location
        self.channel = channel
        self.in_file = in_file    # for testing
        self.listeners = []
    
    def add_listener(self, obj):
        self.listeners.append(weakref.ref(obj))        
                
    def start(self):
        if self.ser is not None:
            self.stop()
        
        if self.in_file is None:
            import serial
            try:
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    timeout=self.timeout)
        
            except serial.serialutil.SerialException:
                logger.error('Cannot open serial port %s' % self.port)
                raise SerialHamsterError('Cannot open serial port %s' % self.port)
        else:
            self.ser = self.in_file

        self.buffer = num.zeros(self.buffersize)

        self.run()
            
    def stop(self):
        if self.ser is not None:
            if self.in_file is None:
                self.ser.close()
            self.ser = None
        
            
    def sun_is_shining(self):
        return True
        
    def run(self):
        while self.process():
            pass
        
        
    def process(self):
        if self.ser is None:
            return False
        
        line = self.ser.readline()
        t = time.time()
        
        for tok in line.split():
            try:
                val = float(tok)
            except:
                logger.warn('Got something unexpected on serial line')
                continue
            
            self.values.append(val)
            self.times.append(t)
            
            if len(self.values) == self.buffersize:
                self._flush_buffer()
        
        return True
        
    def _regression(self,t):
        toff = t[0]
        t = t-toff
        i = num.arange(t.size, dtype=num.float)
        r_deltat, r_tmin, r, tt, stderr = stats.linregress(i, t)
        for ii in range(2):
            t_fit = r_tmin+r_deltat*i
            quickones = num.where(t < t_fit)
            i = i[quickones]
            t = t[quickones]
            r_deltat, r_tmin, r, tt, stderr = stats.linregress(i, t)
        
        return r_deltat, r_tmin+toff
        
    def _flush_buffer(self):
        t = num.array(self.times, dtype=num.float)
        r_deltat, r_tmin = self._regression(t)
        if self.disallow_uneven_sampling_rates:
            r_deltat = 1./round(1./r_deltat)

        # check if deltat is consistent with expectations        
        if self.deltat is not None:
            try:
                p_deltat = self.previous_deltats.median()
                if ((self.disallow_uneven_sampling_rates and abs(1./p_deltat - 1./self.deltat) > 0.5) or
                    (not self.disallow_uneven_sampling_rates and abs((self.deltat - p_deltat)/self.deltat) > self.deltat_tolerance)):
                    self.deltat = None
                    self.previous_deltats.empty()
            except QueueIsEmpty:
                pass
                
        self.previous_deltats.push_back(r_deltat)
        
        # detect sampling rate
        if self.deltat is None:
            self.deltat = r_deltat
            self.tmin = None         # must also set new time origin if sampling rate changes
            logger.info('Setting new sampling rate to %g Hz (sampling interval is %g s)' % (1./self.deltat, self.deltat ))

        # check if onset has drifted / jumped
        if self.tmin is not None and self.tmin is not None:        
            continuous_tmin = self.tmin + self.ncontinuous*self.deltat
            
            tmin_offset = r_tmin - continuous_tmin
            try:
                toffset = self.previous_tmin_offsets.median()
                if abs(toffset) > self.deltat*0.7:
                    soffset = int(round(toffset/self.deltat))
                    logger.info('Detected drift/jump/gap of %g sample%s' % (soffset, ['s',''][abs(soffset)==1]) )
                    if soffset == 1:
                        self.values.append(self.values[-1])
                        self.previous_tmin_offsets.add(-self.deltat)
                        logger.info('Adding one sample to compensate time drift')
                    elif soffset == -1:
                        self.values.pop(-1)
                        self.previous_tmin_offsets.add(+self.deltat)
                        logger.info('Removing one sample to compensate time drift')
                    else:  
                        self.tmin = None
                        self.previous_tmin_offsets.empty()
                    
            except QueueIsEmpty:
                pass
                
            self.previous_tmin_offsets.push_back(tmin_offset)
        
        # detect onset time
        if self.tmin is None:
            self.tmin = r_tmin
            self.ncontinuous = 0
            logger.info('Setting new time origin to %s' % util.gmctime(self.tmin))
        
        v = num.array(self.values, dtype=num.int) 

        tr = trace.Trace(
                network=self.network, 
                station=self.station,
                location=self.location,
                channel=self.channel, 
                tmin=self.tmin + self.ncontinuous*self.deltat, deltat=self.deltat, ydata=v)
                
        self.got_trace(tr)
        self.ncontinuous += v.size
        
        self.values = []
        self.times = []
    
    def got_trace(self, tr):
        logger.debug('Completed trace from serial hamster: %s' % tr)
        
        # deliver payload to registered listeners
        for ref in self.listeners:
            obj = ref()
            if obj:
                obj.insert_trace(trace)
                