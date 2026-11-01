package dev.mrvishwet.adrd.ingest

/** What the device reports about one clip URI, before any transfer touches it. */
data class ClipFields(
    val displayName: String,
    val sizeBytes: Long,
    /** MediaStore DATE_MODIFIED in seconds; null when the URI does not expose it. */
    val lastModifiedSeconds: Long?,
    /** Container mime, straight from MediaMetadataRetriever (video/mp4 for an mp4). */
    val containerMime: String,
    /**
     * Codec of the first video track, from the media framework's extractor. null when
     * the track could not be read. This is the only field that distinguishes HEVC from
     * H.264, because both ship in an mp4 container.
     */
    val videoCodecMime: String?,
    val width: Int,
    val height: Int,
    val bitrateBps: Long,
)

/** Split so resolution logic runs on the host JVM while the reads run on device. */
interface ClipDataSource {
    fun read(uri: String): ClipFields
}

class ClipResolutionException(message: String, cause: Throwable? = null) : Exception(message, cause)

/**
 * Turns a picked content:// URI into a ClipRecord. Device clips are always reached
 * through a content URI - a plain filesystem path is refused rather than silently
 * accepted, because the path can disappear while the upload is in flight.
 */
class ClipResolver(private val source: ClipDataSource) {
    fun resolve(uri: String): ClipRecord {
        if (!ClipUri.isContent(uri)) {
            throw ClipResolutionException("clip must be a content:// URI, was: $uri")
        }
        val fields = try {
            source.read(uri)
        } catch (t: RuntimeException) {
            throw ClipResolutionException("could not read clip metadata for $uri", t)
        }
        val codec = fields.videoCodecMime?.takeIf { it.isNotBlank() } ?: fields.containerMime
        return ClipRecord(
            uri = uri,
            displayName = fields.displayName,
            codecMime = codec,
            width = fields.width,
            height = fields.height,
            bitrateBps = fields.bitrateBps,
            sizeBytes = fields.sizeBytes,
            generationId = ClipGeneration.id(fields.sizeBytes, fields.lastModifiedSeconds),
        )
    }
}

object ClipUri {
    fun isContent(uri: String): Boolean = uri.startsWith("content://")

    /**
     * A picker URI usually names the clip after its row id ("1000000022.mp4"). The
     * MediaStore row holds the camera's real filename, so the row name wins whenever the
     * URI name is nothing but that id.
     */
    fun prefersRowName(openableName: String?, mediaStoreId: Long?): Boolean =
        openableName == null ||
            (mediaStoreId != null && openableName.startsWith("$mediaStoreId."))

    /**
     * The MediaStore row id behind a clip URI, from the trailing path segment. Handles
     * both collection URIs (content://media/external/video/media/22) and document-style
     * ones (…/document/external:video:22). null when there is no numeric tail.
     */
    fun mediaStoreId(uri: String): Long? {
        val last = uri.substringBefore('?').trimEnd('/').substringAfterLast('/', "")
        val tail = last.substringAfterLast(':')
        if (tail.isEmpty() || !tail.all { it.isDigit() }) return null
        return tail.toLongOrNull()
    }
}
