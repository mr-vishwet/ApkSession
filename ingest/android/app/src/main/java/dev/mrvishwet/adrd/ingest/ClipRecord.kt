package dev.mrvishwet.adrd.ingest

import java.io.File

/**
 * One picked device clip: where it came from (a content:// URI), what the device
 * says it is (codec, resolution, bitrate, size) and the generation id that lets an
 * upload notice the file changed underneath it.
 */
data class ClipRecord(
    val uri: String,
    val displayName: String,
    val codecMime: String,
    val width: Int,
    val height: Int,
    val bitrateBps: Long,
    val sizeBytes: Long,
    val generationId: String,
) {
    val codecLabel: String get() = ClipFormat.codecLabel(codecMime)
    val resolution: String get() = if (width > 0 && height > 0) "${width}x$height" else "unknown"
    val isHevc: Boolean get() = codecMime.contains("hevc", ignoreCase = true)
    val isFourK: Boolean get() = maxOf(width, height) >= 3840 && minOf(width, height) >= 2048
}

/** Stable id for the contents a URI pointed at. Changes when the file changes. */
object ClipGeneration {
    fun id(sizeBytes: Long, lastModifiedSeconds: Long?): String =
        if (lastModifiedSeconds == null) "sz$sizeBytes" else "sz$sizeBytes-mt$lastModifiedSeconds"
}

/** Display formatting, kept out of the composables so it can be tested on the JVM. */
object ClipFormat {
    fun codecLabel(mime: String): String = when (mime.lowercase().removePrefix("video/")) {
        "hevc", "hvc1", "hev1" -> "HEVC"
        "avc", "h264" -> "H.264"
        "3gpp", "3gpp2" -> "H.263"
        "mp4", "mp4v-es" -> "MPEG-4"
        "" -> "unknown"
        else -> mime
    }

    fun size(bytes: Long): String = when {
        bytes < 0 -> "unknown"
        bytes < 1024 -> "$bytes B"
        bytes < 1024L * 1024 -> format("%.1f KiB", bytes / 1024.0)
        bytes < 1024L * 1024 * 1024 -> format("%.1f MiB", bytes / (1024.0 * 1024))
        else -> format("%.2f GiB", bytes / (1024.0 * 1024 * 1024))
    }

    fun bitrate(bps: Long): String =
        if (bps <= 0) "unknown" else format("%.1f Mbps", bps / 1_000_000.0)

    private fun format(pattern: String, value: Double): String =
        String.format(java.util.Locale.US, pattern, value)
}

/**
 * The picked clips, persisted so a restart still knows what was selected and can
 * compare each file's generation id before resuming an upload. Same on-disk shape
 * as a checkpoint: key=value lines, records separated by a blank line.
 */
object ClipStore {
    private val KEYS = listOf(
        "uri", "display_name", "codec_mime", "width", "height",
        "bitrate_bps", "size_bytes", "generation_id",
    )

    fun save(file: File, clips: List<ClipRecord>) {
        clips.forEach { clip ->
            listOf(clip.uri, clip.displayName, clip.codecMime, clip.generationId).forEach { value ->
                require(!value.contains('\n') && !value.contains('=')) {
                    "clip string fields must not contain newline or ="
                }
            }
        }
        val text = buildString {
            clips.forEach { clip ->
                append("uri=").append(clip.uri).append('\n')
                append("display_name=").append(clip.displayName).append('\n')
                append("codec_mime=").append(clip.codecMime).append('\n')
                append("width=").append(clip.width).append('\n')
                append("height=").append(clip.height).append('\n')
                append("bitrate_bps=").append(clip.bitrateBps).append('\n')
                append("size_bytes=").append(clip.sizeBytes).append('\n')
                append("generation_id=").append(clip.generationId).append("\n\n")
            }
        }
        AtomicTextFile.write(file, text)
    }

    fun load(file: File): List<ClipRecord> {
        if (!file.exists()) return emptyList()
        val records = file.readText(Charsets.UTF_8).split("\n\n").filter { it.isNotBlank() }
        return records.map { block ->
            val values = mutableMapOf<String, String>()
            block.lineSequence().forEach { line ->
                val i = line.indexOf('=')
                if (i > 0) values[line.substring(0, i)] = line.substring(i + 1)
            }
            val missing = KEYS.filter { !values.containsKey(it) }
            if (missing.isNotEmpty()) throw IllegalStateException("clip record missing keys: $missing")
            ClipRecord(
                uri = values.getValue("uri"),
                displayName = values.getValue("display_name"),
                codecMime = values.getValue("codec_mime"),
                width = values.getValue("width").toIntOrNull()
                    ?: throw IllegalStateException("bad width"),
                height = values.getValue("height").toIntOrNull()
                    ?: throw IllegalStateException("bad height"),
                bitrateBps = values.getValue("bitrate_bps").toLongOrNull()
                    ?: throw IllegalStateException("bad bitrate_bps"),
                sizeBytes = values.getValue("size_bytes").toLongOrNull()
                    ?: throw IllegalStateException("bad size_bytes"),
                generationId = values.getValue("generation_id"),
            )
        }
    }
}
