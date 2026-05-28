// OP-1814 (Track B / B-1) — ONVIF WS-Discovery probe.
//
// Step 1 of the client capability: enumerate ONVIF cameras on the local
// network. WS-Discovery is a SOAP-over-UDP-multicast protocol — we send a
// single `Probe` for the ONVIF NetworkVideoTransmitter device type to the
// well-known group 239.255.255.250:3702 and collect the `ProbeMatch`
// replies for a short window, each of which advertises the device's
// service XAddrs (the SOAP endpoint OnvifMediaClient then talks to).
//
// Manifest note: receiving multicast on Android requires a held
// `WifiManager.MulticastLock`; declare CHANGE_WIFI_MULTICAST_STATE and
// pass a lock factory in (the base skeleton manifest already grants
// INTERNET + ACCESS_NETWORK_STATE). The XML parse is split into the pure
// [parseProbeMatches] so it unit-tests on the JVM with no socket/Android.

package com.omnisight.pilot.onvif

import java.net.DatagramPacket
import java.net.InetAddress
import java.net.MulticastSocket
import java.util.UUID
import javax.xml.parsers.DocumentBuilderFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.w3c.dom.Element
import org.xml.sax.InputSource

/** Multicast group + port fixed by the WS-Discovery spec. */
private const val WS_DISCOVERY_ADDRESS = "239.255.255.250"
private const val WS_DISCOVERY_PORT = 3702

/** ONVIF device type probed for — the standard "network video transmitter". */
private const val NVT_TYPE = "dn:NetworkVideoTransmitter"

/**
 * Held while a multicast probe is in flight. The Android implementation
 * wraps `WifiManager.MulticastLock`; tests pass a no-op. Without it the
 * kernel drops inbound multicast on most devices and discovery returns
 * empty even though cameras replied.
 */
interface MulticastLockHandle {
    fun acquire()
    fun release()
}

/** A [MulticastLockHandle] that does nothing — wired-network / test default. */
object NoopMulticastLock : MulticastLockHandle {
    override fun acquire() {}
    override fun release() {}
}

class WsDiscoveryClient(
    private val lock: MulticastLockHandle = NoopMulticastLock,
    private val timeoutMillis: Int = 4_000,
) {

    /**
     * Multicast a single Probe and gather replies until [timeoutMillis]
     * of socket silence elapses. De-duplicates devices by their
     * EndpointReference so a camera answering on multiple interfaces is
     * reported once.
     */
    suspend fun discover(): List<OnvifDevice> = withContext(Dispatchers.IO) {
        val messageId = "urn:uuid:${UUID.randomUUID()}"
        val probe = buildProbeEnvelope(messageId).toByteArray(Charsets.UTF_8)
        val group = InetAddress.getByName(WS_DISCOVERY_ADDRESS)

        lock.acquire()
        val byEndpoint = LinkedHashMap<String, OnvifDevice>()
        try {
            MulticastSocket().use { socket ->
                socket.soTimeout = timeoutMillis
                socket.send(DatagramPacket(probe, probe.size, group, WS_DISCOVERY_PORT))

                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val packet = DatagramPacket(buffer, buffer.size)
                    try {
                        socket.receive(packet)
                    } catch (_: java.net.SocketTimeoutException) {
                        break // no further replies within the window
                    }
                    val xml = String(packet.data, 0, packet.length, Charsets.UTF_8)
                    for (device in parseProbeMatches(xml)) {
                        byEndpoint.putIfAbsent(device.endpointReference, device)
                    }
                }
            }
        } finally {
            lock.release()
        }
        byEndpoint.values.toList()
    }

    private fun buildProbeEnvelope(messageId: String): String = """
        <?xml version="1.0" encoding="UTF-8"?>
        <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
                    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
                    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
          <e:Header>
            <w:MessageID>$messageId</w:MessageID>
            <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
            <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
          </e:Header>
          <e:Body>
            <d:Probe>
              <d:Types>$NVT_TYPE</d:Types>
            </d:Probe>
          </e:Body>
        </e:Envelope>
    """.trimIndent()

    companion object {
        /**
         * Pure parse of a WS-Discovery `ProbeMatches` document into
         * [OnvifDevice]s. Namespace-agnostic on local names so it tolerates
         * the prefix soup real cameras emit. Returns empty for any
         * non-ProbeMatch payload rather than throwing — discovery should
         * survive one malformed reply among many.
         */
        fun parseProbeMatches(xml: String): List<OnvifDevice> {
            val doc = try {
                val factory = DocumentBuilderFactory.newInstance().apply {
                    isNamespaceAware = true
                }
                factory.newDocumentBuilder().parse(InputSource(xml.reader()))
            } catch (_: Exception) {
                return emptyList()
            }
            val matches = doc.getElementsByTagNameNS("*", "ProbeMatch")
            val devices = ArrayList<OnvifDevice>(matches.length)
            for (i in 0 until matches.length) {
                val match = matches.item(i) as? Element ?: continue
                val endpoint = match.firstText("Address").orEmpty()
                if (endpoint.isBlank()) continue
                val xAddrs = match.firstText("XAddrs")
                    ?.split(Regex("\\s+"))
                    ?.filter { it.isNotBlank() }
                    .orEmpty()
                val scopes = match.firstText("Scopes")
                    ?.split(Regex("\\s+"))
                    ?.filter { it.isNotBlank() }
                    .orEmpty()
                devices += OnvifDevice(endpoint, xAddrs, scopes)
            }
            return devices
        }

        private fun Element.firstText(localName: String): String? {
            val nodes = getElementsByTagNameNS("*", localName)
            return if (nodes.length == 0) null else nodes.item(0).textContent?.trim()
        }
    }
}
