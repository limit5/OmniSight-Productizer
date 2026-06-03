/*
 * [OP-1994] UAC2 gadget ALSA loopback integration helper.
 *
 * The helper is intentionally small and dependency-free: the shell wrapper
 * owns module loading and arecord/aplay execution, while this binary verifies
 * the /dev/snd surface and deterministic raw PCM payloads under qemu-user.
 */

#include <dirent.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define DEFAULT_DEV_ROOT "/dev/snd"
#define DEFAULT_FRAMES 4800U
#define CHANNELS 2U
#define SAMPLE_BYTES 2U

struct options {
	const char *dev_root;
	const char *generate_path;
	const char *verify_path;
	unsigned int frames;
};

struct snd_nodes {
	bool control;
	bool capture;
	bool playback;
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [--dev-root PATH] [--generate RAW] [--verify RAW] "
		"[--frames N] [--self-test]\n",
		prog);
}

static bool has_prefix(const char *name, const char *prefix)
{
	return strncmp(name, prefix, strlen(prefix)) == 0;
}

static int scan_snd_nodes(const char *dev_root, struct snd_nodes *nodes)
{
	struct dirent *entry;
	DIR *dir;

	memset(nodes, 0, sizeof(*nodes));
	dir = opendir(dev_root);
	if (!dir) {
		fprintf(stderr, "uac-loopback: cannot open %s: %s\n",
			dev_root, strerror(errno));
		return -1;
	}

	while ((entry = readdir(dir))) {
		const char *name = entry->d_name;
		size_t len = strlen(name);

		if (has_prefix(name, "controlC"))
			nodes->control = true;
		if (has_prefix(name, "pcmC") && len > 0 && name[len - 1] == 'c')
			nodes->capture = true;
		if (has_prefix(name, "pcmC") && len > 0 && name[len - 1] == 'p')
			nodes->playback = true;
	}
	closedir(dir);

	if (!nodes->control || !nodes->capture || !nodes->playback) {
		fprintf(stderr,
			"uac-loopback: expected control/capture/playback nodes in %s\n",
			dev_root);
		return -1;
	}

	return 0;
}

static int write_pattern(const char *path, unsigned int frames)
{
	FILE *file = fopen(path, "wb");
	unsigned int frame;

	if (!file) {
		fprintf(stderr, "uac-loopback: cannot write %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	for (frame = 0; frame < frames; frame++) {
		int16_t left = (int16_t)((frame * 37U) & 0x7fffU);
		int16_t right = (int16_t)(-left);

		if (fwrite(&left, sizeof(left), 1, file) != 1 ||
		    fwrite(&right, sizeof(right), 1, file) != 1) {
			fprintf(stderr, "uac-loopback: short write to %s\n", path);
			fclose(file);
			return -1;
		}
	}

	if (fclose(file) != 0) {
		fprintf(stderr, "uac-loopback: close failed for %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	return 0;
}

static int verify_pattern(const char *path, unsigned int frames)
{
	FILE *file = fopen(path, "rb");
	unsigned int frame;
	int rc = 0;

	if (!file) {
		fprintf(stderr, "uac-loopback: cannot read %s: %s\n",
			path, strerror(errno));
		return -1;
	}

	for (frame = 0; frame < frames; frame++) {
		int16_t sample[CHANNELS];
		int16_t left = (int16_t)((frame * 37U) & 0x7fffU);
		int16_t right = (int16_t)(-left);

		if (fread(sample, SAMPLE_BYTES, CHANNELS, file) != CHANNELS) {
			fprintf(stderr, "uac-loopback: short read at frame %u\n",
				frame);
			rc = -1;
			break;
		}
		if (sample[0] != left || sample[1] != right) {
			fprintf(stderr,
				"uac-loopback: mismatch at frame %u: %d/%d\n",
				frame, sample[0], sample[1]);
			rc = -1;
			break;
		}
	}

	fclose(file);
	return rc;
}

static int parse_options(int argc, char **argv, struct options *opts)
{
	int i;

	opts->dev_root = DEFAULT_DEV_ROOT;
	opts->generate_path = NULL;
	opts->verify_path = NULL;
	opts->frames = DEFAULT_FRAMES;

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--dev-root") == 0) {
			if (++i >= argc)
				return -1;
			opts->dev_root = argv[i];
		} else if (strcmp(argv[i], "--generate") == 0) {
			if (++i >= argc)
				return -1;
			opts->generate_path = argv[i];
		} else if (strcmp(argv[i], "--verify") == 0) {
			if (++i >= argc)
				return -1;
			opts->verify_path = argv[i];
		} else if (strcmp(argv[i], "--frames") == 0) {
			if (++i >= argc)
				return -1;
			opts->frames = (unsigned int)strtoul(argv[i], NULL, 10);
			if (opts->frames == 0)
				return -1;
		} else if (strcmp(argv[i], "--self-test") == 0) {
			opts->generate_path = opts->verify_path = NULL;
		} else if (strcmp(argv[i], "--help") == 0) {
			usage(argv[0]);
			exit(0);
		} else {
			return -1;
		}
	}

	return 0;
}

int main(int argc, char **argv)
{
	struct options opts;
	struct snd_nodes nodes;

	if (parse_options(argc, argv, &opts) < 0) {
		usage(argv[0]);
		return 2;
	}

	if (scan_snd_nodes(opts.dev_root, &nodes) < 0)
		return 1;
	if (opts.generate_path &&
	    write_pattern(opts.generate_path, opts.frames) < 0)
		return 1;
	if (opts.verify_path && verify_pattern(opts.verify_path, opts.frames) < 0)
		return 1;

	printf("uac-loopback: verified %s (%u frames)\n", opts.dev_root,
	       opts.frames);
	return 0;
}
