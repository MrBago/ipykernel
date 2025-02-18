"""Wrappers for forwarding stdout/stderr over zmq"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import atexit
from binascii import b2a_hex
from collections import deque
from imp import lock_held as import_lock_held
import os
import sys
import threading
import warnings
from weakref import WeakSet
import traceback
from io import StringIO, TextIOBase
import io

import zmq
if zmq.pyzmq_version_info() >= (17, 0):
    from tornado.ioloop import IOLoop
else:
    # deprecated since pyzmq 17
    from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream

from jupyter_client.session import extract_header


#-----------------------------------------------------------------------------
# Globals
#-----------------------------------------------------------------------------

MASTER = 0
CHILD = 1

#-----------------------------------------------------------------------------
# IO classes
#-----------------------------------------------------------------------------


class IOPubThread:
    """An object for sending IOPub messages in a background thread

    Prevents a blocking main thread from delaying output from threads.

    IOPubThread(pub_socket).background_socket is a Socket-API-providing object
    whose IO is always run in a thread.
    """

    def __init__(self, socket, pipe=False):
        """Create IOPub thread

        Parameters
        ----------
        socket : zmq.PUB Socket
            the socket on which messages will be sent.
        pipe : bool
            Whether this process should listen for IOPub messages
            piped from subprocesses.
        """
        self.socket = socket
        self.background_socket = BackgroundSocket(self)
        self._master_pid = os.getpid()
        self._pipe_flag = pipe
        self.io_loop = IOLoop(make_current=False)
        if pipe:
            self._setup_pipe_in()
        self._local = threading.local()
        self._events = deque()
        self._event_pipes = WeakSet()
        self._setup_event_pipe()
        self.thread = threading.Thread(target=self._thread_main, name="IOPub")
        self.thread.daemon = True
        self.thread.pydev_do_not_trace = True
        self.thread.is_pydev_daemon_thread = True
        self.thread.name = "IOPub"

    def _thread_main(self):
        """The inner loop that's actually run in a thread"""
        self.io_loop.make_current()
        self.io_loop.start()
        self.io_loop.close(all_fds=True)

    def _setup_event_pipe(self):
        """Create the PULL socket listening for events that should fire in this thread."""
        ctx = self.socket.context
        pipe_in = ctx.socket(zmq.PULL)
        pipe_in.linger = 0

        _uuid = b2a_hex(os.urandom(16)).decode('ascii')
        iface = self._event_interface = 'inproc://%s' % _uuid
        pipe_in.bind(iface)
        self._event_puller = ZMQStream(pipe_in, self.io_loop)
        self._event_puller.on_recv(self._handle_event)

    @property
    def _event_pipe(self):
        """thread-local event pipe for signaling events that should be processed in the thread"""
        try:
            event_pipe = self._local.event_pipe
        except AttributeError:
            # new thread, new event pipe
            ctx = self.socket.context
            event_pipe = ctx.socket(zmq.PUSH)
            event_pipe.linger = 0
            event_pipe.connect(self._event_interface)
            self._local.event_pipe = event_pipe
            # WeakSet so that event pipes will be closed by garbage collection
            # when their threads are terminated
            self._event_pipes.add(event_pipe)
        return event_pipe

    def _handle_event(self, msg):
        """Handle an event on the event pipe

        Content of the message is ignored.

        Whenever *an* event arrives on the event stream,
        *all* waiting events are processed in order.
        """
        # freeze event count so new writes don't extend the queue
        # while we are processing
        n_events = len(self._events)
        for i in range(n_events):
            event_f = self._events.popleft()
            event_f()

    def _setup_pipe_in(self):
        """setup listening pipe for IOPub from forked subprocesses"""
        ctx = self.socket.context

        # use UUID to authenticate pipe messages
        self._pipe_uuid = os.urandom(16)

        pipe_in = ctx.socket(zmq.PULL)
        pipe_in.linger = 0

        try:
            self._pipe_port = pipe_in.bind_to_random_port("tcp://127.0.0.1")
        except zmq.ZMQError as e:
            warnings.warn("Couldn't bind IOPub Pipe to 127.0.0.1: %s" % e +
                "\nsubprocess output will be unavailable."
            )
            self._pipe_flag = False
            pipe_in.close()
            return
        self._pipe_in = ZMQStream(pipe_in, self.io_loop)
        self._pipe_in.on_recv(self._handle_pipe_msg)

    def _handle_pipe_msg(self, msg):
        """handle a pipe message from a subprocess"""
        if not self._pipe_flag or not self._is_master_process():
            return
        if msg[0] != self._pipe_uuid:
            print("Bad pipe message: %s", msg, file=sys.__stderr__)
            return
        self.send_multipart(msg[1:])

    def _setup_pipe_out(self):
        # must be new context after fork
        ctx = zmq.Context()
        pipe_out = ctx.socket(zmq.PUSH)
        pipe_out.linger = 3000 # 3s timeout for pipe_out sends before discarding the message
        pipe_out.connect("tcp://127.0.0.1:%i" % self._pipe_port)
        return ctx, pipe_out

    def _is_master_process(self):
        return os.getpid() == self._master_pid

    def _check_mp_mode(self):
        """check for forks, and switch to zmq pipeline if necessary"""
        if not self._pipe_flag or self._is_master_process():
            return MASTER
        else:
            return CHILD

    def start(self):
        """Start the IOPub thread"""
        self.thread.name = "IOPub"
        self.thread.start()
        # make sure we don't prevent process exit
        # I'm not sure why setting daemon=True above isn't enough, but it doesn't appear to be.
        atexit.register(self.stop)

    def stop(self):
        """Stop the IOPub thread"""
        if not self.thread.is_alive():
            return
        self.io_loop.add_callback(self.io_loop.stop)
        self.thread.join()
        # close *all* event pipes, created in any thread
        # event pipes can only be used from other threads while self.thread.is_alive()
        # so after thread.join, this should be safe
        for event_pipe in self._event_pipes:
            event_pipe.close()

    def close(self):
        if self.closed:
            return
        self.socket.close()
        self.socket = None

    @property
    def closed(self):
        return self.socket is None

    def schedule(self, f):
        """Schedule a function to be called in our IO thread.

        If the thread is not running, call immediately.
        """
        if self.thread.is_alive():
            self._events.append(f)
            # wake event thread (message content is ignored)
            self._event_pipe.send(b'')
        else:
            f()

    def send_multipart(self, *args, **kwargs):
        """send_multipart schedules actual zmq send in my thread.

        If my thread isn't running (e.g. forked process), send immediately.
        """
        self.schedule(lambda : self._really_send(*args, **kwargs))

    def _really_send(self, msg, *args, **kwargs):
        """The callback that actually sends messages"""
        mp_mode = self._check_mp_mode()

        if mp_mode != CHILD:
            # we are master, do a regular send
            self.socket.send_multipart(msg, *args, **kwargs)
        else:
            # we are a child, pipe to master
            # new context/socket for every pipe-out
            # since forks don't teardown politely, use ctx.term to ensure send has completed
            ctx, pipe_out = self._setup_pipe_out()
            pipe_out.send_multipart([self._pipe_uuid] + msg, *args, **kwargs)
            pipe_out.close()
            ctx.term()


class BackgroundSocket:
    """Wrapper around IOPub thread that provides zmq send[_multipart]"""
    io_thread = None

    def __init__(self, io_thread):
        self.io_thread = io_thread

    def __getattr__(self, attr):
        """Wrap socket attr access for backward-compatibility"""
        if attr.startswith('__') and attr.endswith('__'):
            # don't wrap magic methods
            super().__getattr__(attr)
        if hasattr(self.io_thread.socket, attr):
            warnings.warn(
                f"Accessing zmq Socket attribute {attr} on BackgroundSocket"
                f" is deprecated since ipykernel 4.3.0"
                f" use .io_thread.socket.{attr}",
                DeprecationWarning,
                stacklevel=2,
            )
            return getattr(self.io_thread.socket, attr)
        super().__getattr__(attr)

    def __setattr__(self, attr, value):
        if attr == 'io_thread' or (attr.startswith('__' and attr.endswith('__'))):
            super().__setattr__(attr, value)
        else:
            warnings.warn(
                f"Setting zmq Socket attribute {attr} on BackgroundSocket"
                f" is deprecated since ipykernel 4.3.0"
                f" use .io_thread.socket.{attr}",
                DeprecationWarning,
                stacklevel=2,
            )
            setattr(self.io_thread.socket, attr, value)

    def send(self, msg, *args, **kwargs):
        return self.send_multipart([msg], *args, **kwargs)

    def send_multipart(self, *args, **kwargs):
        """Schedule send in IO thread"""
        return self.io_thread.send_multipart(*args, **kwargs)


class OutStream(TextIOBase):
    """A file like object that publishes the stream to a 0MQ PUB socket.

    Output is handed off to an IO Thread
    """

    # timeout for flush to avoid infinite hang
    # in case of misbehavior
    flush_timeout = 10
    # The time interval between automatic flushes, in seconds.
    flush_interval = 0.2
    topic = None
    encoding = 'UTF-8'


    def fileno(self):
        """
        Things like subprocess will peak and write to the fileno() of stderr/stdout.
        """
        if getattr(self, "_original_stdstream_copy", None) is not None:
            return self._original_stdstream_copy
        else:
            raise io.UnsupportedOperation("fileno")

    def _watch_pipe_fd(self):
        """
        We've redirected standards steams 0 and 1 into a pipe.

        We need to watch in a thread and redirect them to the right places.

        1) the ZMQ channels to show in notebook interfaces,
        2) the original stdout/err, to capture errors in terminals.

        We cannot schedule this on the ioloop thread, as this might be blocking.

        """

        try:
            bts = os.read(self._fid, 1000)
            while bts and self._should_watch:
                self.write(bts.decode())
                os.write(self._original_stdstream_copy, bts)
                bts = os.read(self._fid, 1000)
        except Exception:
            self._exc = sys.exc_info()

    def __init__(
        self, session, pub_thread, name, pipe=None, echo=None, *, watchfd=True, isatty=False,
    ):
        """
        Parameters
        ----------
        name : str {'stderr', 'stdout'}
            the name of the standard stream to replace
        watchfd : bool (default, True)
            Watch the file descripttor corresponding to the replaced stream.
            This is useful if you know some underlying code will write directly
            the file descriptor by its number. It will spawn a watching thread,
            that will swap the give file descriptor for a pipe, read from the
            pipe, and insert this into the current Stream.
        isatty : bool (default, False)
            Indication of whether this stream has termimal capabilities (e.g. can handle colors)

        """
        if pipe is not None:
            warnings.warn(
                "pipe argument to OutStream is deprecated and ignored",
                " since ipykernel 4.2.3.",
                DeprecationWarning,
                stacklevel=2,
            )
        # This is necessary for compatibility with Python built-in streams
        self.session = session
        if not isinstance(pub_thread, IOPubThread):
            # Backward-compat: given socket, not thread. Wrap in a thread.
            warnings.warn(
                "Since IPykernel 4.3, OutStream should be created with "
                "IOPubThread, not %r" % pub_thread,
                DeprecationWarning,
                stacklevel=2,
            )
            pub_thread = IOPubThread(pub_thread)
            pub_thread.start()
        self.pub_thread = pub_thread
        self.name = name
        self.topic = b"stream." + name.encode()
        self.parent_header = {}
        self._master_pid = os.getpid()
        self._flush_pending = False
        self._subprocess_flush_pending = False
        self._io_loop = pub_thread.io_loop
        self._new_buffer()
        self.echo = None
        self._isatty = bool(isatty)

        if (
            watchfd
            and (sys.platform.startswith("linux") or sys.platform.startswith("darwin"))
            and ("PYTEST_CURRENT_TEST" not in os.environ)
        ):
            # Pytest set its own capture. Dont redirect from within pytest.

            self._should_watch = True
            self._setup_stream_redirects(name)

        if echo:
            if hasattr(echo, 'read') and hasattr(echo, 'write'):
                self.echo = echo
            else:
                raise ValueError("echo argument must be a file like object")

    def isatty(self):
        """Return a bool indicating whether this is an 'interactive' stream.

        Returns:
            Boolean
        """
        return self._isatty

    def _setup_stream_redirects(self, name):
        pr, pw = os.pipe()
        fno = getattr(sys, name).fileno()
        self._original_stdstream_copy = os.dup(fno)
        os.dup2(pw, fno)

        self._fid = pr

        self._exc = None
        self.watch_fd_thread = threading.Thread(target=self._watch_pipe_fd)
        self.watch_fd_thread.daemon = True
        self.watch_fd_thread.start()

    def _is_master_process(self):
        return os.getpid() == self._master_pid

    def set_parent(self, parent):
        self.parent_header = extract_header(parent)

    def close(self):
        if sys.platform.startswith("linux") or sys.platform.startswith("darwin"):
            self._should_watch = False
            self.watch_fd_thread.join()
        if self._exc:
            etype, value, tb = self._exc
            traceback.print_exception(etype, value, tb)
        self.pub_thread = None

    @property
    def closed(self):
        return self.pub_thread is None

    def _schedule_flush(self):
        """schedule a flush in the IO thread

        call this on write, to indicate that flush should be called soon.
        """
        if self._flush_pending:
            return
        self._flush_pending = True

        # add_timeout has to be handed to the io thread via event pipe
        def _schedule_in_thread():
            self._io_loop.call_later(self.flush_interval, self._flush)
        self.pub_thread.schedule(_schedule_in_thread)

    def flush(self):
        """trigger actual zmq send

        send will happen in the background thread
        """
        if (
                self.pub_thread
                and self.pub_thread.thread is not None
                and self.pub_thread.thread.is_alive()
                and self.pub_thread.thread.ident != threading.current_thread().ident
        ):
            # request flush on the background thread
            self.pub_thread.schedule(self._flush)
            # wait for flush to actually get through, if we can.
            # waiting across threads during import can cause deadlocks
            # so only wait if import lock is not held
            if not import_lock_held():
                evt = threading.Event()
                self.pub_thread.schedule(evt.set)
                # and give a timeout to avoid
                if not evt.wait(self.flush_timeout):
                    # write directly to __stderr__ instead of warning because
                    # if this is happening sys.stderr may be the problem.
                    print("IOStream.flush timed out", file=sys.__stderr__)
        else:
            self._flush()

    def _flush(self):
        """This is where the actual send happens.

        _flush should generally be called in the IO thread,
        unless the thread has been destroyed (e.g. forked subprocess).
        """
        self._flush_pending = False
        self._subprocess_flush_pending = False

        if self.echo is not None:
            try:
                self.echo.flush()
            except OSError as e:
                if self.echo is not sys.__stderr__:
                    print(f"Flush failed: {e}",
                          file=sys.__stderr__)

        data = self._flush_buffer()
        if data:
            # FIXME: this disables Session's fork-safe check,
            # since pub_thread is itself fork-safe.
            # There should be a better way to do this.
            self.session.pid = os.getpid()
            content = {'name':self.name, 'text':data}
            self.session.send(self.pub_thread, 'stream', content=content,
                parent=self.parent_header, ident=self.topic)

    def write(self, string: str) -> int:
        """Write to current stream after encoding if necessary

        Returns
        -------
        len : int
            number of items from input parameter written to stream.

        """

        if not isinstance(string, str):
            raise TypeError(
                f"write() argument must be str, not {type(string)}"
            )

        if self.echo is not None:
            try:
                self.echo.write(string)
            except OSError as e:
                if self.echo is not sys.__stderr__:
                    print(f"Write failed: {e}",
                          file=sys.__stderr__)

        if self.pub_thread is None:
            raise ValueError('I/O operation on closed file')
        else:

            is_child = (not self._is_master_process())
            # only touch the buffer in the IO thread to avoid races
            self.pub_thread.schedule(lambda: self._buffer.write(string))
            if is_child:
                # mp.Pool cannot be trusted to flush promptly (or ever),
                # and this helps.
                if self._subprocess_flush_pending:
                    return
                self._subprocess_flush_pending = True
                # We can not rely on self._io_loop.call_later from a subprocess
                self.pub_thread.schedule(self._flush)
            else:
                self._schedule_flush()

        return len(string)

    def writelines(self, sequence):
        if self.pub_thread is None:
            raise ValueError('I/O operation on closed file')
        else:
            for string in sequence:
                self.write(string)

    def writable(self):
        return True

    def _flush_buffer(self):
        """clear the current buffer and return the current buffer data.

        This should only be called in the IO thread.
        """
        data = ''
        if self._buffer is not None:
            buf = self._buffer
            self._new_buffer()
            data = buf.getvalue()
            buf.close()
        return data

    def _new_buffer(self):
        self._buffer = StringIO()
