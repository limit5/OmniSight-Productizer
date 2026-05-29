// Case-1 ONVIF/RTSP client home screen (pack overlay over the skill-android
// base HomeScreen). Discovers ONVIF cameras on the LAN via WS-Discovery, lets
// the user pick one, resolves its RTSP stream URI over the ONVIF Media service,
// and hands off to RtspPlayerScreen for playback.
package com.omnisight.pilot.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.omnisight.pilot.onvif.OnvifDevice
import com.omnisight.pilot.onvif.OnvifMediaClient
import com.omnisight.pilot.onvif.WifiMulticastLock
import com.omnisight.pilot.onvif.WsDiscoveryClient
import com.omnisight.pilot.rtsp.RtspPlayerScreen
import kotlinx.coroutines.launch

@Composable
fun HomeScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var status by remember { mutableStateOf("Discovering ONVIF cameras on the LAN…") }
    var devices by remember { mutableStateOf<List<OnvifDevice>>(emptyList()) }
    var streamUri by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        runCatching { WsDiscoveryClient(WifiMulticastLock(context)).discover() }
            .onSuccess { found ->
                devices = found
                status = if (found.isEmpty()) {
                    "No ONVIF cameras found on this network."
                } else {
                    "Found ${found.size} camera(s) — tap one to play."
                }
            }
            .onFailure { status = "Discovery failed: ${it.message}" }
    }

    val uri = streamUri
    if (uri != null) {
        RtspPlayerScreen(rtspUri = uri, modifier = Modifier.fillMaxSize())
        return
    }

    Scaffold { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(24.dp)
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(text = "OnvifClient", style = MaterialTheme.typography.headlineMedium)
            Text(text = status, style = MaterialTheme.typography.bodyLarge)
            devices.forEach { device ->
                val address = device.primaryServiceAddress
                Button(
                    modifier = Modifier.fillMaxWidth(),
                    enabled = address != null,
                    onClick = {
                        address?.let { serviceUrl ->
                            status = "Resolving stream from $serviceUrl…"
                            scope.launch {
                                runCatching {
                                    val media = OnvifMediaClient(serviceUrl)
                                    val token = media.getProfiles().firstOrNull()?.token
                                        ?: error("camera returned no media profiles")
                                    media.getStreamUri(token).uri
                                }
                                    .onSuccess { resolved -> streamUri = resolved }
                                    .onFailure { status = "Stream resolve failed: ${it.message}" }
                            }
                        }
                    },
                ) { Text(text = address ?: device.endpointReference) }
            }
        }
    }
}
