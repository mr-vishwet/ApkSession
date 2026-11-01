package dev.mrvishwet.adrd.ingest

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.Build
import android.provider.BaseColumns
import android.provider.MediaStore
import android.provider.OpenableColumns

/**
 * Device-side reads behind ClipResolver: the content provider for name/size/date and
 * MediaMetadataRetriever for the codec, frame size and bitrate. Nothing here assumes a
 * filesystem path exists; every read goes through the URI the picker returned.
 */
class AndroidClipDataSource(private val context: Context) : ClipDataSource {

    private data class ProviderRow(
        val displayName: String?,
        val sizeBytes: Long,
        val dateModifiedSeconds: Long?,
        val mime: String?,
    )

    private data class RetrieverTags(
        val mime: String?,
        val width: Int,
        val height: Int,
        val bitrate: Long,
    )

    override fun read(uri: String): ClipFields {
        val parsed = Uri.parse(uri)
        val openable = query(parsed, ITEM_PROJECTION)
        val rowId = ClipUri.mediaStoreId(uri)
        val needsRow = openable == null ||
            openable.dateModifiedSeconds == null ||
            openable.sizeBytes < 0 ||
            ClipUri.prefersRowName(openable.displayName, rowId)
        val row = if (needsRow) mediaStoreRow(parsed) else null
        val tags = retrieverTags(parsed)

        val sizeBytes = when {
            openable != null && openable.sizeBytes >= 0 -> openable.sizeBytes
            row != null && row.sizeBytes >= 0 -> row.sizeBytes
            else -> descriptorLength(parsed)
        }
        val containerMime = tags.mime?.takeIf { it.isNotBlank() }
            ?: openable?.mime?.takeIf { it.isNotBlank() }
            ?: row?.mime?.takeIf { it.isNotBlank() }
            ?: ""

        return ClipFields(
            displayName = row?.displayName?.takeIf { it.isNotBlank() }
                ?: openable?.displayName?.takeIf { it.isNotBlank() }
                ?: parsed.lastPathSegment
                ?: "clip",
            sizeBytes = sizeBytes,
            lastModifiedSeconds = openable?.dateModifiedSeconds ?: row?.dateModifiedSeconds,
            containerMime = containerMime,
            videoCodecMime = trackCodec(parsed),
            width = tags.width,
            height = tags.height,
            bitrateBps = tags.bitrate,
        )
    }

    /**
     * Fields the picked URI itself exposes. Unsupported columns come back absent rather
     * than throwing, so each one is read by index and guarded.
     */
    private fun query(uri: Uri, projection: Array<String>): ProviderRow? = runCatching {
        context.contentResolver.query(uri, projection, null, null, null)
    }.getOrNull()?.use { cursor ->
        if (!cursor.moveToFirst()) return@use null
        ProviderRow(
            displayName = cursor.text(OpenableColumns.DISPLAY_NAME),
            sizeBytes = cursor.long(OpenableColumns.SIZE) ?: -1L,
            dateModifiedSeconds = cursor.long(MediaStore.MediaColumns.DATE_MODIFIED),
            mime = cursor.text(MediaStore.MediaColumns.MIME_TYPE),
        )
    }

    /**
     * Fallback for URIs that do not expose DATE_MODIFIED/SIZE: read the MediaStore row by
     * id, which does need READ_MEDIA_VIDEO. Without the permission this returns null and
     * the generation id falls back to size alone - no crash, no all-files request.
     */
    private fun mediaStoreRow(uri: Uri): ProviderRow? {
        val id = ClipUri.mediaStoreId(uri.toString()) ?: return null
        if (!hasVideoReadPermission()) return null
        val selection = "${BaseColumns._ID} = ?"
        return runCatching {
            context.contentResolver.query(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                MEDIA_ROW_PROJECTION,
                selection,
                arrayOf(id.toString()),
                null,
            )
        }.getOrNull()?.use { cursor ->
            if (!cursor.moveToFirst()) return@use null
            ProviderRow(
                displayName = cursor.text(MediaStore.MediaColumns.DISPLAY_NAME),
                sizeBytes = cursor.long(MediaStore.MediaColumns.SIZE) ?: -1L,
                dateModifiedSeconds = cursor.long(MediaStore.MediaColumns.DATE_MODIFIED),
                mime = cursor.text(MediaStore.MediaColumns.MIME_TYPE),
            )
        }
    }

    private fun hasVideoReadPermission(): Boolean {
        val permission = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Manifest.permission.READ_MEDIA_VIDEO
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        return context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    }

    private fun descriptorLength(uri: Uri): Long = runCatching {
        context.contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length }
    }.getOrNull() ?: -1L

    /**
     * Codec, frame size and bitrate, read through the URI's file descriptor so they work
     * for provider URIs that carry no readable path.
     */
    private fun retrieverTags(uri: Uri): RetrieverTags {
        val retriever = MediaMetadataRetriever()
        return try {
            context.contentResolver.openFileDescriptor(uri, "r")?.use { afd ->
                retriever.setDataSource(afd.fileDescriptor)
            }
            RetrieverTags(
                mime = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_MIMETYPE),
                width = retriever.integer(MEDIA_WIDTH_KEY),
                height = retriever.integer(MEDIA_HEIGHT_KEY),
                bitrate = retriever.long(MEDIA_BITRATE_KEY) ?: 0L,
            )
        } catch (t: Exception) {
            RetrieverTags(null, 0, 0, 0)
        } finally {
            runCatching { retriever.release() }
        }
    }

    /**
     * The codec of the first video track. MediaMetadataRetriever only reports the
     * container mime, so an HEVC clip in an mp4 reads back as "video/mp4" and would be
     * indistinguishable from an H.264 one; the extractor's track format is what actually
     * names the codec.
     */
    private fun trackCodec(uri: Uri): String? {
        val extractor = MediaExtractor()
        var codec: String? = null
        try {
            extractor.setDataSource(context, uri, null)
            for (i in 0 until extractor.trackCount) {
                val mime = extractor.getTrackFormat(i).getString(MediaFormat.KEY_MIME)
                if (mime != null && mime.startsWith("video/")) {
                    codec = mime
                    break
                }
            }
        } catch (t: Exception) {
            codec = null
        } finally {
            runCatching { extractor.release() }
        }
        return codec
    }

    private fun Cursor.text(column: String): String? {
        val i = getColumnIndex(column)
        return if (i < 0 || isNull(i)) null else getString(i)
    }

    private fun Cursor.long(column: String): Long? {
        val i = getColumnIndex(column)
        return if (i < 0 || isNull(i)) null else getLong(i)
    }

    private fun MediaMetadataRetriever.integer(key: Int): Int =
        extractMetadata(key)?.toIntOrNull() ?: 0

    private fun MediaMetadataRetriever.long(key: Int): Long? =
        extractMetadata(key)?.toLongOrNull()

    companion object {
        private val ITEM_PROJECTION = arrayOf(
            OpenableColumns.DISPLAY_NAME,
            OpenableColumns.SIZE,
            MediaStore.MediaColumns.DATE_MODIFIED,
            MediaStore.MediaColumns.MIME_TYPE,
        )
        private val MEDIA_ROW_PROJECTION = arrayOf(
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.DATE_MODIFIED,
            MediaStore.MediaColumns.MIME_TYPE,
        )
        private const val MEDIA_WIDTH_KEY = MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH
        private const val MEDIA_HEIGHT_KEY = MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT
        private const val MEDIA_BITRATE_KEY = MediaMetadataRetriever.METADATA_KEY_BITRATE
    }
}
