package dev.mrvishwet.adrd.ingest

import java.io.File
import java.io.FileOutputStream
import java.io.IOException

/**
 * Writes text so that a reader can only ever see the old complete file or the new
 * complete file: temp file, flush, fsync, then rename over the target. Rename is
 * atomic on Linux/Android, and the temp name is unique so concurrent writers cannot
 * collide on it.
 */
internal object AtomicTextFile {
    fun write(file: File, text: String) {
        val dir = file.absoluteFile.parentFile
        dir.mkdirs()
        val tmp = File(dir, file.name + ".tmp." + System.nanoTime())
        try {
            FileOutputStream(tmp).use { fos ->
                fos.write(text.toByteArray(Charsets.UTF_8))
                fos.flush()
                fos.fd.sync()
            }
            if (!tmp.renameTo(file)) throw IOException("rename failed for ${file.name}")
        } catch (t: Throwable) {
            tmp.delete()
            throw t
        }
    }
}
