import logging
logger = logging.getLogger('abs.suit.server')

import SocketServer
from lib import thread2 as threading
import select
import Queue
try: 
    from cStringIO import StringIO as sio
except ImportError:
    from StringIO import StringIO as sio
import traceback    
import sys
import struct
import string
import json
import time

#local
import lib.cfg

#abs layer imports, used for the creation of objects and finding the obj list for the relevent items
from . import suit

#dict for "objtype byte" to (class,dict obj)
objtype = {\
          "s":(suit.suit,suit.suits),#suits
          "a":(None,None),#area tiles
          "g":(None,None),#gameobjects 
          "d":(None,None)#data accessors
          }


class abs_con_handler(SocketServer.BaseRequestHandler):
    '''handle a reconnecting object, put new descriptor in the relevent connection dict, if the relevent list does not have the relevant OID create new.'''
    def __init__(self, request, client_address, server):
        self.request = request
        self.client_address = client_address
        self.server = server
        
        try:
            self.setup()
            self.handle()
            self.finish()
        except Exception:
            #log exception and end connection handler
            self.finnish_ex()
        finally:
            sys.exc_traceback = None    # Help garbage collection?
            #no matter what, if we are here, it is time to clear our netobj from the relevent connection dict
            #look into possibly creating a dummy netobj that wont crash the other objects?
            if self.OID != None:
                logger.debug('removed connection from suit "%s"'%self.OID)
                suit.suits[self.OID][1]=None
    
    def setup(self):
        '''
        create queue's and get the OID. place handler in suits
        
        queue's are in self.qu{first_tpye:queue}
        '''
        #suit version: 16 char string representing version, start with 'lzr'
        self.OID=None#start workable
        self.suitversion=self.request.recv(16)
        if not self.suitversion.startswith('lzr'):
            raise Excption('connection not from suit to suit server!')
        
        #OID is the second thing sent over the wire
        self.OID=struct.unpack('q',self.request.recv(8))[0]
        #after OID is the single object type byte
        self.objtype = self.request.recv(1)
        
        logger.info('handling new suit connection from:%s\n \
                     suit software version............:%s\n \
                     suit ID..........................:%s'%(self.client_address,self.suitversion,self.OID))
        
        #set up queue's
        self.incomingq = Queue.Queue(10) #read from suit
        self.outgoingq= Queue.Queue(10) #headed to suit
        
        
        #set timeout for network latency
        self.request.settimeout(0.5)
        
        #set up and start write thread
        self.write_thread = threading.Thread(target=self.writer)
        self.write_thread.setDaemon(True)
        self.write_thread.start()
        
        
        
        
        if self.OID in self.get_objlist() and self.get_objlist()[self.OID][1] is not None:
            logger.warn('new connection for %s is already connected, overwritting old with new'%self.OID)
            #as quickly as possible set up the new connection, after set up then we close the old connection
            #TODO:: find if an error in closing old will block/error the new connection
            old_conn=self.get_objlist()[self.OID][1]
            self.get_objlist()[self.OID][1]=self
            old_conn.close()
        else:
            logger.info('new netobj object being created for %s'%self.OID)
            s=self.get_objclass()(self.OID)
            self.get_objlist()[self.OID]=[s,self]
            
    def get_objlist(self):
        '''always used to make refrences as weak as possible without weakref's
        returns the obj dict (meaning it is not actually a list!)
        '''
        return objtype[self.objtype][1]
        
    def get_objclass(self):
        '''always used to make references as weak as possible without weakref's'''
        return objtype[self.objtype][0]
        
    def get_objinst(self):
        '''get the object instance for this net instance, we dont store the ref because we want to be weak incase of problems'''
        return self.get_objlist()[self.OID][0]
        
    def close(self):
        self.write_thread.terminate()
        self.run_handler=False
        time.sleep(0.25)#wait for the handlers to close normaly, but we can force it as well...
        self.request.close()#and if the handler is still open, this kills it with socket errors
        
    def handle(self):
        self.run_handler=True
        while self.run_handler:
            readready,writeready,exceptionready = select.select([self.request],[],[],0.25)
            for streamobj in readready:
                if streamobj == self.request:
                    self.handle_one()
                    
    def handle_one(self):
        header = self.request.recv(8)
        content_len = struct.unpack('I',header[:4])[0]
        #see description above for header layout
        short_func = header[4:]
        for ch in short_func:
            if ch not in string.ascii_letters:
                raise Exception('received bad call function, must be ascii_letters. got:"%s"'%short_func)
        ##read data in 1024 byte chunks, but once under, use actual size
        if content_len >1024:
            tcon = content_len
            data = []
            while tcon > 1024:
                data.append(self.request.recv(1024))
                tcon = tcon-1024
            data.append(self.request.recv(tcon))
            data = ''.join(data)
        else:
            data = self.request.recv(content_len)
        print (short_func,data)
        jdata=json.loads(data)#must always have json data, of none/invalid let loads die
        #pass to for suit to read and act upon
        print (short_func,jdata)
        self.get_objinst().run_packet(short_func,jdata)
        
    def make_packet(self,action,data):
        '''
        this function is broken out so others beyond the writer can use it
        packet def:
            '####xxxx'
            4 chars of number, being packet size packed using struct.pack('I',####)
            4 chars of ASCII letters, to either:
                if from suit: to translate into function names (eg: 'ghit'==def got_hit(self,weapon)...)
                    here data would be the json object representing the weapon
                if from server: action name for suit to do (eg, 'chst'==changestats)
                    here data would be something like: {'health':('-',5)} #loose five health
        '''
        if len(action) !=4:
            raise Exception('action must be 4 chars.')
        if not isinstance(data, basestring):
            data = json.dumps(data)
        header=struct.pack('I',len(data))+action
        return header+data
    
    def writer(self):
        '''eats things from the outgoing queue.
        format of outgoing data: tuple(action,jsonabledata)'''
        while True:
            short_func,data=self.outgoingq.get()
            packet=self.make_packet(short_func,data)
            ##todo::: add to stack for reliability the logging of all data out
            self.request.sendall(packet)
    def finnish(self):
        logger.warn('OBJECT %s requested connection closed.'%self.OID)
        
        
    def finnish_ex(self):
        buff=sio()
        traceback.print_exc(file=buff)
        if self.OID is not None:
            buff.write('OBJECT ID: %s\n'%self.OID)
        logger.error('abs_network communication error! %s'%buff.getvalue())
        buff.close()
        del buff#stupid GC hates me
        

abs_server=None
abs_server_thread=None
def init():
    global abs_server
    global abs_server_thread
    def run_server():
        try:
            abs_server.serve_forever()
        finally:
            abs_server.server.close()
    #set up our server. doesnt start yet, only when abs.suit.init() is called
    abs_server=SocketServer.ThreadingTCPServer((lib.cfg.main['abs_net_server']['host'],lib.cfg.main['abs_net_server'].as_int('port')), abs_con_handler)
    abs_server.daemon_threads = True
    abs_server_thread = threading.Thread(target=run_server)
    abs_server_thread.setDaemon(True)#start in new thread as to not hang the main thread in case we want console acsess (ipython?)