/*
 * PCL Stream Generator — OmniSight Print Pipeline (C20)
 *
 * Generates PCL 5e/5c/6-XL output from raster image data.
 * Handles page setup, raster transfer, duplex, and color modes.
 *
 * Build: gcc -o pcl_generator pcl_generator.c -lm
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* PCL escape sequences */
#define PCL_RESET           "\x1bE"
#define PCL_PAGE_SIZE       "\x1b&l%dA"     /* 2=letter, 26=A4 */
#define PCL_ORIENTATION     "\x1b&l%dO"     /* 0=portrait, 1=landscape */
#define PCL_COPIES          "\x1b&l%dX"
#define PCL_DUPLEX          "\x1b&l%dS"     /* 0=simplex, 1=long, 2=short */
#define PCL_RESOLUTION      "\x1b*t%dR"
#define PCL_RASTER_START    "\x1b*r1A"
#define PCL_RASTER_ROW      "\x1b*b%dW"
#define PCL_RASTER_END      "\x1b*rB"
#define PCL_FORM_FEED       "\x0c"

typedef struct {
    int    page_size;     /* PCL code: 2=letter, 26=A4, 27=A3 */
    int    orientation;   /* 0=portrait, 1=landscape */
    int    resolution;    /* DPI */
    int    copies;
    int    duplex;        /* 0=simplex, 1=long-edge, 2=short-edge */
    int    color;         /* 0=mono, 1=color */
} pcl_config_t;

typedef struct {
    uint8_t *data;
    int      width;
    int      height;
    int      bpp;         /* bytes per pixel */
} raster_page_t;

static int pcl_write_header(FILE *out, const pcl_config_t *cfg) {
    fprintf(out, PCL_RESET);
    fprintf(out, PCL_PAGE_SIZE, cfg->page_size);
    fprintf(out, PCL_ORIENTATION, cfg->orientation);
    fprintf(out, PCL_RESOLUTION, cfg->resolution);
    fprintf(out, PCL_COPIES, cfg->copies);
    fprintf(out, PCL_DUPLEX, cfg->duplex);
    return 0;
}

static int pcl_write_raster_page(FILE *out, const raster_page_t *page) {
    int row_bytes = page->width * page->bpp;
    fprintf(out, PCL_RASTER_START);
    for (int y = 0; y < page->height; y++) {
        fprintf(out, PCL_RASTER_ROW, row_bytes);
        fwrite(page->data + y * row_bytes, 1, row_bytes, out);
    }
    fprintf(out, PCL_RASTER_END);
    fprintf(out, PCL_FORM_FEED);
    return 0;
}

static int pcl_write_footer(FILE *out) {
    fprintf(out, PCL_RESET);
    return 0;
}

int pcl_generate(FILE *out, const pcl_config_t *cfg,
                 const raster_page_t *pages, int page_count) {
    pcl_write_header(out, cfg);
    for (int i = 0; i < page_count; i++) {
        pcl_write_raster_page(out, &pages[i]);
    }
    pcl_write_footer(out);
    return 0;
}
