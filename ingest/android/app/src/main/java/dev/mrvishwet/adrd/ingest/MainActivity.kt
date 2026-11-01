package dev.mrvishwet.adrd.ingest

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { IngestScreen() }
    }
}

@Composable
fun IngestScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val resolver = remember { ClipResolver(AndroidClipDataSource(context)) }
    val clipFile = remember { File(context.filesDir, "picked_clips.txt") }
    val scope = rememberCoroutineScope()

    var clips by remember { mutableStateOf(ClipStore.load(clipFile)) }
    var status by remember { mutableStateOf("") }

    val pickVideo = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri: Uri? ->
        if (uri == null) {
            status = "picker closed without a selection"
            return@rememberLauncherForActivityResult
        }
        status = "reading clip metadata…"
        scope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) { resolver.resolve(uri.toString()) }
            }
            result.onSuccess { clip ->
                clips = clips.filterNot { it.uri == clip.uri } + clip
                ClipStore.save(clipFile, clips)
                status = ""
            }.onFailure { status = "could not read clip: ${it.message}" }
        }
    }

    // READ_MEDIA_VIDEO is not what unlocks the picker; it unlocks the MediaStore row
    // lookup used when a picked URI does not expose its own date/size columns. The
    // picker opens either way.
    val askVideoPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { pickVideo.launch(videoPickRequest()) }

    val startPick: () -> Unit = {
        if (videoPermissionMissing(context)) {
            askVideoPermission.launch(Manifest.permission.READ_MEDIA_VIDEO)
        } else {
            pickVideo.launch(videoPickRequest())
        }
    }

    Scaffold(modifier = modifier.fillMaxSize()) { innerPadding ->
        Column(
            modifier = Modifier
                .padding(innerPadding)
                .fillMaxSize()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("adrd ingest", style = MaterialTheme.typography.headlineSmall)
            Button(onClick = startPick, modifier = Modifier.fillMaxWidth()) {
                Text("Pick a device video")
            }
            if (status.isNotEmpty()) {
                Text(status, style = MaterialTheme.typography.bodySmall)
            }
            if (clips.isEmpty()) {
                Text(
                    "No clips picked yet. Picked clips list codec, resolution, bitrate and " +
                        "byte size here before anything is transferred.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(clips, key = { it.uri }) { clip -> ClipCard(clip) }
            }
        }
    }
}

@Composable
private fun ClipCard(clip: ClipRecord) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    clip.displayName,
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.weight(1f),
                )
                if (clip.isFourK && clip.isHevc) {
                    Text(
                        "4K HEVC",
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.primary,
                    )
                }
            }
            Text(
                "${clip.codecLabel} - ${clip.resolution} - " +
                    "${ClipFormat.bitrate(clip.bitrateBps)} - ${ClipFormat.size(clip.sizeBytes)}",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                "generation ${clip.generationId}",
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

private fun videoPickRequest(): PickVisualMediaRequest = PickVisualMediaRequest(
    ActivityResultContracts.PickVisualMedia.VideoOnly
)

private fun videoPermissionMissing(context: Context): Boolean =
    Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
        context.checkSelfPermission(Manifest.permission.READ_MEDIA_VIDEO) !=
        PackageManager.PERMISSION_GRANTED
