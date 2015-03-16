#
# Copyright 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import threading
import time
import select
import errno
from ceilometer.openstack.common import log
from eventlet import greenthread


LOG = log.getLogger(__name__)

# How many times a reconnect should be performed before a cooldown will be
# applied
COOLDOWN_RECONNECT_THRESHOLD = 5


# NOTE: it would be best to try and unify NoIntrCall and NoIntrPoll.
# We could do so defining a new object that can be used as a placeholer
# for the changing timeout value in the *args/**kwargs. This would
# lead us to rebuilding the function arguments at each loop.
def NoIntrPoll(pollfun, timeout=-1):
    """
    This wrapper is used to handle the interrupt exceptions that might
    occur during a poll system call. The wrapped function must be defined
    as poll([timeout]) where the special timeout value 0 is used to return
    immediately and -1 is used to wait indefinitely.
    """
    # When the timeout < 0 we shouldn't compute a new timeout after an
    # interruption.
    endtime = None if timeout < 0 else time.time() + timeout

    while True:
        try:
            return pollfun(timeout)
        except (IOError, select.error) as e:
            if e.args[0] != errno.EINTR:
                raise

        if endtime is not None:
            timeout = max(0, endtime - time.time())

        # Note(luogangyi): Since native thread is replaced by eventlet,
        # we must add sleep here to switch green thread manually.
        greenthread.sleep(1)


class Listener(threading.Thread):
    """
    An events driven listener which handle messages from virtual machines.
    """
    def __init__(self):
        threading.Thread.__init__(self, name='VM Channels Listener')
        self.daemon = True
        self._quit = False
        self._epoll = select.epoll()
        self._channels = {}
        self._unconnected = {}
        self._update_lock = threading.Lock()
        self._add_channels = {}
        self._del_channels = []
        self._timeout = None

    def _handle_event(self, fileno, event):
        """ Handle an epoll event occurred on a specific file descriptor. """
        reconnect = False
        if (event & (select.EPOLLHUP | select.EPOLLERR)):
            LOG.info("Received %.08X on fileno %d", event, fileno)
            if fileno in self._channels:
                reconnect = True
            else:
                LOG.debug("Received %.08X. On fd removed by epoll.",
                               event)
        elif (event & select.EPOLLIN):
            obj = self._channels.get(fileno, None)
            if obj:
                obj['timeout_seen'] = False
                obj['reconnects'] = 0
                try:
                    if obj['read_cb'](obj['opaque']):
                        obj['read_time'] = time.time()
                    else:
                        reconnect = True
                except:
                    LOG.exception("Exception on read callback.")
            else:
                LOG.debug("Received epoll event %.08X for no longer "
                               "tracked fd = %d", event, fileno)

        if reconnect:
            self._prepare_reconnect(fileno)

    def _prepare_reconnect(self, fileno):
            obj = self._channels.pop(fileno)
            obj['timeout_seen'] = False
            try:
                fileno = obj['create_cb'](obj['opaque'])
            except:
                LOG.exception("An error occurred in the create callback "
                                   "fileno: %d.", fileno)
            else:
                self._unconnected[fileno] = obj

    def _handle_timeouts(self):
        """
        Scan channels and notify registered client if a timeout occurred on
        their file descriptor.
        """
        now = time.time()
        for (fileno, obj) in self._channels.items():
            if (now - obj['read_time']) >= self._timeout:
                if not obj.get('timeout_seen', False):
                    LOG.debug("Timeout on fileno %d.", fileno)
                    obj['timeout_seen'] = True
                try:
                    obj['timeout_cb'](obj['opaque'])
                    obj['read_time'] = now
                except:
                    LOG.exception("Exception on timeout callback.")

    def _do_add_channels(self):
        """ Add new channels to unconnected channels list. """
        for (fileno, obj) in self._add_channels.items():
            LOG.debug("fileno %d was added to unconnected channels.",
                           fileno)
            self._unconnected[fileno] = obj
        self._add_channels.clear()

    def _do_del_channels(self):
        """ Remove requested channels from listener. """
        for fileno in self._del_channels:
            self._add_channels.pop(fileno, None)
            self._unconnected.pop(fileno, None)
            self._channels.pop(fileno, None)
            LOG.debug("fileno %d was removed from listener.", fileno)
        self._del_channels = []

    def _update_channels(self):
        """ Update channels list. """
        with self._update_lock:
            self._do_add_channels()
            self._do_del_channels()

    def _handle_unconnected(self):
        """
        Scan the unconnected channels and give the registered client a chance
        to connect their channel.
        """
        now = time.time()
        for (fileno, obj) in self._unconnected.items():
            if obj.get('cooldown'):
                if (now - obj['cooldown_time']) >= self._timeout:
                    obj['cooldown'] = False
                    LOG.info("Reconnect attempt fileno "
                                 "%d", fileno)
                else:
                    continue

            try:
                success = obj['connect_cb'](obj['opaque'])
            except:
                LOG.exception("Exception on connect callback.")
            else:
                if success:
                    LOG.debug("Connecting to fileno %d succeeded.",
                                   fileno)
                    del self._unconnected[fileno]
                    self._channels[fileno] = obj
                    obj['read_time'] = time.time()
                    self._epoll.register(fileno, select.EPOLLIN)
                else:
                    obj['reconnects'] = obj.get('reconnects', 0) + 1
                    if obj['reconnects'] >= COOLDOWN_RECONNECT_THRESHOLD:
                        obj['cooldown_time'] = time.time()
                        obj['cooldown'] = True
                        LOG.info("fileno %d was moved into "
                                     "cooldown", fileno)

    def _wait_for_events(self):
        """ Wait for an epoll event and handle channels' timeout. """
        events = NoIntrPoll(self._epoll.poll, 1)
        for (fileno, event) in events:
            self._handle_event(fileno, event)
        else:
            self._update_channels()
            if (self._timeout is not None) and (self._timeout > 0):
                self._handle_timeouts()
            self._handle_unconnected()

    def run(self):
        """ The listener thread's function. """
        LOG.info("Starting VM channels listener thread.")
        self._quit = False
        try:
            while not self._quit:
                self._wait_for_events()
                # Note(luogangyi): Since native thread is replaced by eventlet,
                # we must add sleep here to switch green thread manually.
                greenthread.sleep(1)
        except:
            LOG.exception("Unhandled exception caught in vm channels "
                               "listener thread")
        LOG.info("VM channels listener thread has ended.")

    def stop(self):
        """" Stop the listener execution. """
        self._quit = True
        LOG.info("VM channels listener was stopped.")

    def settimeout(self, seconds):
        """ Set the timeout value (in seconds) for all channels. """
        LOG.info("Setting channels' timeout to %d seconds.", seconds)
        self._timeout = seconds

    def register(self, create_callback, connect_callback, read_callback,
                 timeout_callback, opaque):
        """ Register a new file descriptor to the listener. """
        fileno = create_callback(opaque)
        LOG.debug("Add fileno %d to listener's channels.", fileno)
        with self._update_lock:
            self._add_channels[fileno] = {
                'connect_cb': connect_callback,
                'read_cb': read_callback, 'timeout_cb': timeout_callback,
                'opaque': opaque, 'create_cb': create_callback,
                'read_time': 0.0,
            }

    def unregister(self, fileno):
        """ Unregister an exist file descriptor from the listener. """
        LOG.debug("Delete fileno %d from listener.", fileno)
        with self._update_lock:
            self._del_channels.append(fileno)

