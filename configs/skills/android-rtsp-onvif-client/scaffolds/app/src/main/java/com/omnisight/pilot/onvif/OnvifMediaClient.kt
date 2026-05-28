// OP-1814 (Track B / B-1) — ONVIF Device/Media SOAP client.
//
// Step 2 of the client capability: turn a discovered device-service XAddr
// (from WsDiscoveryClient) into a playable RTSP URI. Two SOAP calls
// against the ONVIF Media service:
//
//   GetProfiles   → the media profiles the camera exposes (token + name)
//   GetStreamUri  → the RTSP URI for a chosen profile token
//
// Most cameras require authentication, so each request carries a
// WS-Security UsernameToken with a SHA-1 PasswordDigest (nonce + UTC
// created + password), which is the ONVIF default. Network I/O uses
// HttpURLConnection (no extra dependency); the envelope builders and
// response parsers are pure + testable on the JVM.

package com.omnisight.pilot.onvif

import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import javax.xml.parsers.DocumentBuilderFactory
import kotlin.io.encoding.Base64
import kotlin.io.encoding.ExperimentalEncodingApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.w3c.dom.Element
import org.xml.sax.InputSource

@OptIn(ExperimentalEncodingApi::class)
class OnvifMediaClient(
    private val serviceUrl: String,
    private val credentials: OnvifCredentials? = null,
    private val connectTimeoutMillis: Int = 5_000,
    private val readTimeoutMillis: Int = 5_000,
) {

    /** Fetch the camera's media profiles. */
    suspend fun getProfiles(): List<MediaProfile> {
        val response = post(buildGetProfiles())
        return parseProfiles(response)
    }

    /**
     * Resolve the RTSP stream URI for [profileToken]. Requests
     * RTP-Unicast over RTSP, the transport every RTSP player (incl.
     * Media3) speaks.
     */
    suspend fun getStreamUri(profileToken: String): StreamUri {
        val response = post(buildGetStreamUri(profileToken))
        return parseStreamUri(response)
    }

    // ── transport ──────────────────────────────────────────────────

    private suspend fun post(body: String): String = withContext(Dispatchers.IO) {
        val conn = (URL(serviceUrl).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = connectTimeoutMillis
            readTimeout = readTimeoutMillis
            doOutput = true
            setRequestProperty(
                "Content-Type",
                "application/soap+xml; charset=utf-8",
            )
        }
        try {
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val stream = if (conn.responseCode in 200..299) {
                conn.inputStream
            } else {
                conn.errorStream ?: error("ONVIF call failed: HTTP ${conn.responseCode}")
            }
            stream.bufferedReader(Charsets.UTF_8).use { it.readText() }
        } finally {
            conn.disconnect()
        }
    }

    // ── envelope builders ──────────────────────────────────────────

    private fun buildGetProfiles(): String = envelope(
        "<trt:GetProfiles/>",
    )

    private fun buildGetStreamUri(profileToken: String): String = envelope(
        """
        <trt:GetStreamUri>
          <trt:StreamSetup>
            <tt:Stream>RTP-Unicast</tt:Stream>
            <tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>
          </trt:StreamSetup>
          <trt:ProfileToken>$profileToken</trt:ProfileToken>
        </trt:GetStreamUri>
        """.trimIndent(),
    )

    private fun envelope(bodyInner: String): String {
        val header = credentials?.let { securityHeader(it) } ?: ""
        return """
            <?xml version="1.0" encoding="UTF-8"?>
            <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                        xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
                        xmlns:tt="http://www.onvif.org/ver10/schema">
              <s:Header>$header</s:Header>
              <s:Body>$bodyInner</s:Body>
            </s:Envelope>
        """.trimIndent()
    }

    /** WS-Security UsernameToken with SHA-1 PasswordDigest (ONVIF default). */
    private fun securityHeader(creds: OnvifCredentials): String {
        val nonceBytes = ByteArray(16).also { java.security.SecureRandom().nextBytes(it) }
        val created = utcNow()
        val digest = passwordDigest(nonceBytes, created, creds.password)
        val nonceB64 = Base64.encode(nonceBytes)
        return """
            <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
                           xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
              <wsse:UsernameToken>
                <wsse:Username>${creds.username}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">$digest</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">$nonceB64</wsse:Nonce>
                <wsu:Created>$created</wsu:Created>
              </wsse:UsernameToken>
            </wsse:Security>
        """.trimIndent()
    }

    companion object {
        /** Parse a `GetProfilesResponse` into [MediaProfile]s. */
        fun parseProfiles(xml: String): List<MediaProfile> {
            val doc = parse(xml) ?: return emptyList()
            val nodes = doc.getElementsByTagNameNS("*", "Profiles")
            val out = ArrayList<MediaProfile>(nodes.length)
            for (i in 0 until nodes.length) {
                val el = nodes.item(i) as? Element ?: continue
                val token = el.getAttribute("token").ifBlank {
                    el.getAttribute("ProfileToken")
                }
                if (token.isBlank()) continue
                val name = el.firstText("Name").orEmpty().ifBlank { token }
                out += MediaProfile(token = token, name = name)
            }
            return out
        }

        /** Parse a `GetStreamUriResponse` into a [StreamUri]. */
        fun parseStreamUri(xml: String): StreamUri {
            val doc = parse(xml) ?: error("malformed GetStreamUri response")
            val mediaUri = doc.getElementsByTagNameNS("*", "MediaUri")
            val container: Element = (mediaUri.item(0) as? Element)
                ?: (doc.documentElement)
                ?: error("GetStreamUri response had no MediaUri")
            val uri = container.firstText("Uri")
                ?: error("GetStreamUri response had no Uri")
            return StreamUri(
                uri = uri,
                invalidAfterConnect = container.firstText("InvalidAfterConnect")
                    .toBoolean(),
                invalidAfterReboot = container.firstText("InvalidAfterReboot")
                    .toBoolean(),
            )
        }

        /** SHA-1( nonce + created + password ), Base64 — the ONVIF digest. */
        @OptIn(ExperimentalEncodingApi::class)
        fun passwordDigest(nonce: ByteArray, created: String, password: String): String {
            val sha1 = MessageDigest.getInstance("SHA-1")
            sha1.update(nonce)
            sha1.update(created.toByteArray(Charsets.UTF_8))
            sha1.update(password.toByteArray(Charsets.UTF_8))
            return Base64.encode(sha1.digest())
        }

        fun utcNow(): String {
            val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
                timeZone = TimeZone.getTimeZone("UTC")
            }
            return fmt.format(Date())
        }

        private fun parse(xml: String): org.w3c.dom.Document? = try {
            DocumentBuilderFactory.newInstance()
                .apply { isNamespaceAware = true }
                .newDocumentBuilder()
                .parse(InputSource(xml.reader()))
        } catch (_: Exception) {
            null
        }

        private fun Element.firstText(localName: String): String? {
            val nodes = getElementsByTagNameNS("*", localName)
            return if (nodes.length == 0) null else nodes.item(0).textContent?.trim()
        }

        private fun String?.toBoolean(): Boolean = this?.trim()?.lowercase() == "true"
    }
}
