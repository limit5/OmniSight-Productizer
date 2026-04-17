# SKILL-ANDROID (P8 #293) — project-specific ProGuard / R8 rules.
#
# The default `proguard-android-optimize.txt` (applied via app/build.gradle.kts)
# keeps the standard AndroidX / Compose surface. Add app-specific keeps
# here (Gson / Moshi / Room model classes, reflection-based libraries).

# Keep Kotlin metadata so reflection-based features (sealed class names,
# data class component accessors) survive R8.
-keep class kotlin.Metadata { *; }

# Keep Compose runtime's generated classes (mostly covered by the BOM,
# but the rule makes the intent explicit).
-keep class androidx.compose.runtime.** { *; }

# Keep our own application subclass so it instantiates via reflection.
-keep public class * extends android.app.Application
