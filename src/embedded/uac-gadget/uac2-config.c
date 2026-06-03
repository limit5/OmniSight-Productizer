/* SPDX-License-Identifier: MIT
 *
 * USB UAC2 gadget configfs validator (OP-1985).
 *
 * This helper checks the kernel configfs state created by
 * scripts/embedded/setup_uac2_gadget.sh. It deliberately stops at kernel
 * gadget attributes; ALSA PCM routing belongs to C5.A.2.
 */
#include "uac2-config.h"

#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int make_path(char *buf, size_t buf_len, const char *base,
		     const char *child)
{
	int written;

	written = snprintf(buf, buf_len, "%s/%s", base, child);
	if (written < 0 || (size_t)written >= buf_len)
		return -ENAMETOOLONG;

	return 0;
}

static int make_gadget_path(char *buf, size_t buf_len,
			    const struct omnisight_uac2_config *config)
{
	return make_path(buf, buf_len, config->configfs_root,
			 config->gadget_name);
}

static int read_text_file(const char *path, char *buf, size_t buf_len)
{
	FILE *file;
	size_t len;

	if (buf_len == 0)
		return -EINVAL;

	file = fopen(path, "r");
	if (!file)
		return -errno;

	if (!fgets(buf, buf_len, file)) {
		int saved_errno = ferror(file) ? errno : ENODATA;

		fclose(file);
		return -saved_errno;
	}

	fclose(file);
	len = strcspn(buf, "\r\n");
	buf[len] = '\0';
	return 0;
}

static int require_directory(const char *path)
{
	struct stat st;

	if (stat(path, &st) != 0) {
		fprintf(stderr, "uac2-config: missing %s: %s\n", path,
			strerror(errno));
		return -errno;
	}

	if (!S_ISDIR(st.st_mode)) {
		fprintf(stderr, "uac2-config: %s is not a directory\n", path);
		return -ENOTDIR;
	}

	return 0;
}

static int require_file_value(const char *path, const char *expected)
{
	char actual[OMNISIGHT_UAC2_MAX_TEXT];
	int rc;

	rc = read_text_file(path, actual, sizeof(actual));
	if (rc != 0) {
		fprintf(stderr, "uac2-config: cannot read %s: %s\n", path,
			strerror(-rc));
		return rc;
	}

	if (strcmp(actual, expected) != 0) {
		fprintf(stderr,
			"uac2-config: %s expected %s, got %s\n",
			path, expected, actual);
		return -EINVAL;
	}

	return 0;
}

static int require_symlink_target_contains(const char *path,
					   const char *needle)
{
	char target[OMNISIGHT_UAC2_MAX_PATH];
	ssize_t len;

	len = readlink(path, target, sizeof(target) - 1);
	if (len < 0) {
		fprintf(stderr, "uac2-config: missing link %s: %s\n", path,
			strerror(errno));
		return -errno;
	}

	target[len] = '\0';
	if (strstr(target, needle) == NULL) {
		fprintf(stderr,
			"uac2-config: %s target %s does not contain %s\n",
			path, target, needle);
		return -EINVAL;
	}

	return 0;
}

static int require_function_attr(const char *function_path, const char *name,
				 const char *expected)
{
	char path[OMNISIGHT_UAC2_MAX_PATH];
	int rc;

	rc = make_path(path, sizeof(path), function_path, name);
	if (rc != 0)
		return rc;

	return require_file_value(path, expected);
}

static int validate_uac2_function(const struct omnisight_uac2_config *config,
				  const char *gadget_path)
{
	char function_path[OMNISIGHT_UAC2_MAX_PATH];
	char rel_path[OMNISIGHT_UAC2_MAX_PATH];
	int rc;

	rc = snprintf(rel_path, sizeof(rel_path), "functions/%s",
		      config->function_name);
	if (rc < 0 || (size_t)rc >= sizeof(rel_path))
		return -ENAMETOOLONG;

	rc = make_path(function_path, sizeof(function_path), gadget_path,
		       rel_path);
	if (rc != 0)
		return rc;

	rc = require_directory(function_path);
	if (rc != 0)
		return rc;

	rc = require_function_attr(function_path, "c_srate",
				   config->sample_rate);
	if (rc != 0)
		return rc;
	rc = require_function_attr(function_path, "p_srate",
				   config->sample_rate);
	if (rc != 0)
		return rc;
	rc = require_function_attr(function_path, "c_ssize",
				   config->sample_size);
	if (rc != 0)
		return rc;
	rc = require_function_attr(function_path, "p_ssize",
				   config->sample_size);
	if (rc != 0)
		return rc;
	rc = require_function_attr(function_path, "c_chmask",
				   config->channel_mask);
	if (rc != 0)
		return rc;

	return require_function_attr(function_path, "p_chmask",
				     config->channel_mask);
}

static int validate_config_link(const struct omnisight_uac2_config *config,
				const char *gadget_path)
{
	char config_path[OMNISIGHT_UAC2_MAX_PATH];
	char link_path[OMNISIGHT_UAC2_MAX_PATH];
	char rel_path[OMNISIGHT_UAC2_MAX_PATH];
	int rc;

	rc = snprintf(rel_path, sizeof(rel_path), "configs/%s",
		      config->config_name);
	if (rc < 0 || (size_t)rc >= sizeof(rel_path))
		return -ENAMETOOLONG;

	rc = make_path(config_path, sizeof(config_path), gadget_path, rel_path);
	if (rc != 0)
		return rc;

	rc = require_directory(config_path);
	if (rc != 0)
		return rc;

	rc = make_path(link_path, sizeof(link_path), config_path,
		       config->function_name);
	if (rc != 0)
		return rc;

	return require_symlink_target_contains(link_path, config->function_name);
}

static int validate_udc(const struct omnisight_uac2_config *config,
			const char *gadget_path)
{
	char path[OMNISIGHT_UAC2_MAX_PATH];
	char udc[OMNISIGHT_UAC2_MAX_TEXT];
	int rc;

	if (!config->require_bound_udc)
		return 0;

	rc = make_path(path, sizeof(path), gadget_path, "UDC");
	if (rc != 0)
		return rc;

	rc = read_text_file(path, udc, sizeof(udc));
	if (rc != 0) {
		fprintf(stderr, "uac2-config: cannot read %s: %s\n", path,
			strerror(-rc));
		return rc;
	}

	if (udc[0] == '\0') {
		fprintf(stderr, "uac2-config: gadget is not bound to a UDC\n");
		return -ENODEV;
	}

	return 0;
}

int omnisight_uac2_default_config(struct omnisight_uac2_config *config)
{
	if (!config)
		return -EINVAL;

	config->configfs_root = OMNISIGHT_UAC2_DEFAULT_CONFIGFS_ROOT;
	config->gadget_name = OMNISIGHT_UAC2_DEFAULT_GADGET_NAME;
	config->function_name = OMNISIGHT_UAC2_DEFAULT_FUNCTION_NAME;
	config->config_name = OMNISIGHT_UAC2_DEFAULT_CONFIG_NAME;
	config->sample_rate = OMNISIGHT_UAC2_DEFAULT_SAMPLE_RATE;
	config->sample_size = OMNISIGHT_UAC2_DEFAULT_SAMPLE_SIZE;
	config->channel_mask = OMNISIGHT_UAC2_DEFAULT_CHANNEL_MASK;
	config->require_bound_udc = 1;
	return 0;
}

int omnisight_uac2_validate(const struct omnisight_uac2_config *config)
{
	char gadget_path[OMNISIGHT_UAC2_MAX_PATH];
	int rc;

	if (!config || !config->configfs_root || !config->gadget_name ||
	    !config->function_name || !config->config_name ||
	    !config->sample_rate || !config->sample_size ||
	    !config->channel_mask)
		return -EINVAL;

	rc = make_gadget_path(gadget_path, sizeof(gadget_path), config);
	if (rc != 0)
		return rc;

	rc = require_directory(gadget_path);
	if (rc != 0)
		return rc;
	rc = validate_uac2_function(config, gadget_path);
	if (rc != 0)
		return rc;
	rc = validate_config_link(config, gadget_path);
	if (rc != 0)
		return rc;

	return validate_udc(config, gadget_path);
}

static void print_usage(const char *program)
{
	fprintf(stderr,
		"usage: %s [--configfs-root PATH] [--gadget NAME] "
		"[--function NAME] [--config NAME] [--allow-unbound]\n",
		program);
}

int main(int argc, char **argv)
{
	struct omnisight_uac2_config config;
	int i;
	int rc;

	omnisight_uac2_default_config(&config);

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--configfs-root") == 0 && i + 1 < argc) {
			config.configfs_root = argv[++i];
		} else if (strcmp(argv[i], "--gadget") == 0 && i + 1 < argc) {
			config.gadget_name = argv[++i];
		} else if (strcmp(argv[i], "--function") == 0 && i + 1 < argc) {
			config.function_name = argv[++i];
		} else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) {
			config.config_name = argv[++i];
		} else if (strcmp(argv[i], "--allow-unbound") == 0) {
			config.require_bound_udc = 0;
		} else if (strcmp(argv[i], "--help") == 0) {
			print_usage(argv[0]);
			return 0;
		} else {
			print_usage(argv[0]);
			return 2;
		}
	}

	rc = omnisight_uac2_validate(&config);
	if (rc != 0)
		return 1;

	printf("uac2-config: %s/%s validated\n", config.configfs_root,
	       config.gadget_name);
	return 0;
}
