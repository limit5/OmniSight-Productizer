/* dag-executor smoke fixture (OP-1673).
 *
 * Minimal host-native "firmware": prints a recognisable banner and exits 0
 * so the cmake compile task produces a real, runnable build/firmware.bin
 * artifact for the downstream run-test task to consume. Intentionally tiny —
 * this is a build/run smoke seed, not a functional firmware image.
 */
#include <stdio.h>

int main(void)
{
	printf("dag-smoke-fixture firmware OK\n");
	return 0;
}
