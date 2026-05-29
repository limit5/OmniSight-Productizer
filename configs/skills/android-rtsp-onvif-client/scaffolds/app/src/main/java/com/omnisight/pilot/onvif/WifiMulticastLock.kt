// Android-backed MulticastLockHandle for WS-Discovery. Android drops inbound
// multicast to save power unless a WifiManager.MulticastLock is held, so the
// ONVIF Probe replies would never arrive without this on real Wi-Fi.
package com.omnisight.pilot.onvif

import android.content.Context
import android.net.wifi.WifiManager

class WifiMulticastLock(context: Context) : MulticastLockHandle {
    private val lock = (context.applicationContext
        .getSystemService(Context.WIFI_SERVICE) as WifiManager)
        .createMulticastLock("onvif-ws-discovery")
        .apply { setReferenceCounted(true) }

    override fun acquire() {
        lock.acquire()
    }

    override fun release() {
        if (lock.isHeld) {
            lock.release()
        }
    }
}
