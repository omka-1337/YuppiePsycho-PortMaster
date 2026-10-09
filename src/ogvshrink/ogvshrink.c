/*
 * ogvshrink - re-encode the video of an Ogg Theora file at a smaller size.
 *
 *   ogvshrink [options] IN OUT    shrink IN into OUT
 *   ogvshrink --bench IN          only decode IN and report how long it takes
 *
 * Options:
 *   --max WxH         fit the picture inside WxH, keeping its aspect (default 640x480)
 *   --quality Q       Theora quality 0-63 (default 44)
 *   --speed S         encoder speed level 0-4, higher is faster (default 3; 4 turns
 *                     off motion compensation, which makes files and decoding bigger)
 *   --min-shrink F    leave a video alone unless its width shrinks below F times
 *                     the original (default 0.9)
 *   --no-verify       do not decode OUT again after writing it
 *
 * Exit status: 0 OUT written, 3 nothing to do (the reason is printed), 1 error.
 * Results are printed as key=value lines (read by ypport.py).
 *
 * Software Theora decoding cost grows with the number of pixels, and a
 * Cortex-A53 cannot decode high resolution cutscenes in real time; a 640x480
 * screen does not show the extra pixels anyway. The Theora stream is decoded,
 * scaled down with an area filter and encoded again with libtheora. Vorbis
 * audio streams are copied page by page, untouched, interleaved with the new
 * video pages by time; other streams (Ogg Skeleton), which Theorafile
 * ignores, are left out. Duplicate-frame packets are kept as
 * duplicates, so the output has exactly as many frames as the input.
 */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <ogg/ogg.h>
#include <theora/theoradec.h>
#include <theora/theoraenc.h>

#define EXIT_SKIP 3

static void die(const char *fmt, const char *arg)
{
	fprintf(stderr, "ogvshrink: ");
	fprintf(stderr, fmt, arg);
	fprintf(stderr, "\n");
	exit(1);
}

static void *xmalloc(size_t n)
{
	void *p = calloc(1, n ? n : 1);
	if (p == NULL)
		die("out of memory%s", "");
	return p;
}

static double now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

/* -- per-frame decode timing --------------------------------------------- */

typedef struct {
	float *ms;
	size_t n, cap;
} timing;

static void timing_add(timing *t, double ms)
{
	if (t->n == t->cap) {
		t->cap = t->cap ? t->cap * 2 : 1024;
		t->ms = realloc(t->ms, t->cap * sizeof(float));
		if (t->ms == NULL)
			die("out of memory%s", "");
	}
	t->ms[t->n++] = (float)ms;
}

static int cmp_float(const void *a, const void *b)
{
	float x = *(const float *)a, y = *(const float *)b;
	return (x > y) - (x < y);
}

/* Prints avg, 95th percentile and max decode time per frame, and the share
 * of frames that took longer than a frame lasts. */
static void timing_print(const char *prefix, timing *t, double fps)
{
	double sum = 0, budget = fps > 0 ? 1000.0 / fps : 0;
	size_t i, slow = 0;
	if (t->n == 0)
		return;
	for (i = 0; i < t->n; i++) {
		sum += t->ms[i];
		if (budget > 0 && t->ms[i] > budget)
			slow++;
	}
	qsort(t->ms, t->n, sizeof(float), cmp_float);
	printf("%s_decode_avg_ms=%.2f\n", prefix, sum / t->n);
	printf("%s_decode_p95_ms=%.2f\n", prefix, t->ms[(t->n * 95) / 100]);
	printf("%s_decode_max_ms=%.2f\n", prefix, t->ms[t->n - 1]);
	printf("%s_slow_frames_pct=%.1f\n", prefix, 100.0 * slow / t->n);
}

/* -- Ogg input ------------------------------------------------------------ */

static int read_page(FILE *f, ogg_sync_state *oy, ogg_page *og)
{
	int r;
	while ((r = ogg_sync_pageout(oy, og)) != 1) {
		char *buf;
		size_t n;
		if (r < 0)
			continue; /* skipped garbage, try again */
		buf = ogg_sync_buffer(oy, 65536);
		n = fread(buf, 1, 65536, f);
		if (n == 0)
			return 0;
		ogg_sync_wrote(oy, (long)n);
	}
	return 1;
}

/* -- area-filter scaling -------------------------------------------------- */

/* Weights (sum 1 << 14) of the source samples covering each output sample. */
typedef struct {
	int src_n, dst_n, taps;
	int *first; /* first source sample of each output sample */
	int *w;     /* dst_n * taps weights, zero padded */
} axis;

static void axis_init(axis *a, int src_n, int dst_n)
{
	int i, j;
	a->src_n = src_n;
	a->dst_n = dst_n;
	a->taps = 1;
	/* A source sample spans dst_n units, an output sample src_n units. */
	for (i = 0; i < dst_n; i++) {
		long lo = (long)i * src_n, hi = (long)(i + 1) * src_n;
		int n = (int)((hi - 1) / dst_n - lo / dst_n) + 1;
		if (n > a->taps)
			a->taps = n;
	}
	a->first = xmalloc(sizeof(int) * dst_n);
	a->w = xmalloc(sizeof(int) * dst_n * a->taps);
	for (i = 0; i < dst_n; i++) {
		long lo = (long)i * src_n, hi = (long)(i + 1) * src_n;
		int j0 = (int)(lo / dst_n), j1 = (int)((hi - 1) / dst_n), sum = 0;
		int *w = a->w + i * a->taps;
		a->first[i] = j0;
		for (j = j0; j <= j1; j++) {
			long s0 = (long)j * dst_n, s1 = s0 + dst_n;
			long ov = (hi < s1 ? hi : s1) - (lo > s0 ? lo : s0);
			w[j - j0] = (int)((ov * 16384 + src_n / 2) / src_n);
			sum += w[j - j0];
		}
		w[j1 - j0] += 16384 - sum;
	}
}

/* Taps of output sample i that lie inside the source. */
static int axis_taps(const axis *a, int i)
{
	int n = a->src_n - a->first[i];
	return n < a->taps ? n : a->taps;
}

static void axis_free(axis *a)
{
	free(a->first);
	free(a->w);
}

/* Vertical pass first: it works on whole rows, which vectorises, and leaves
 * the horizontal pass (a gather per sample) a third or less of the rows. */
typedef struct {
	int sx, sy, sw, sh; /* source region */
	int dw, dh;
	axis ax, ay;
	int *row; /* one vertically filtered row of sw samples, 8.6 fixed point */
} scaler;

static void scaler_init(scaler *s, int sx, int sy, int sw, int sh, int dw, int dh)
{
	s->sx = sx; s->sy = sy; s->sw = sw; s->sh = sh; s->dw = dw; s->dh = dh;
	axis_init(&s->ax, sw, dw);
	axis_init(&s->ay, sh, dh);
	s->row = xmalloc(sizeof(int) * (size_t)sw);
}

static void scaler_free(scaler *s)
{
	axis_free(&s->ax);
	axis_free(&s->ay);
	free(s->row);
}

static void scaler_run(scaler *s, const th_img_plane *src, unsigned char *dst, int dst_stride)
{
	int x, y, k, sw = s->sw;
	int *row = s->row;
	for (y = 0; y < s->dh; y++) {
		const int *wy = s->ay.w + y * s->ay.taps;
		int ny = axis_taps(&s->ay, y);
		const unsigned char *in = src->data + (ptrdiff_t)(s->sy + s->ay.first[y]) * src->stride + s->sx;
		unsigned char *out = dst + (ptrdiff_t)y * dst_stride;
		for (x = 0; x < sw; x++)
			row[x] = wy[0] * in[x];
		for (k = 1; k < ny; k++) {
			const unsigned char *r = in + (ptrdiff_t)k * src->stride;
			int w = wy[k];
			for (x = 0; x < sw; x++)
				row[x] += w * r[x];
		}
		for (x = 0; x < sw; x++)
			row[x] = (row[x] + 128) >> 8;
		for (x = 0; x < s->dw; x++) {
			const int *wx = s->ax.w + x * s->ax.taps;
			const int *p = row + s->ax.first[x];
			int nx = axis_taps(&s->ax, x), acc = 0;
			for (k = 0; k < nx; k++)
				acc += wx[k] * p[k];
			acc = (acc + (1 << 19)) >> 20;
			out[x] = (unsigned char)(acc < 0 ? 0 : acc > 255 ? 255 : acc);
		}
	}
}

/* -- page queue for the streams that are copied --------------------------- */

typedef struct {
	unsigned char *data;
	long len;
	double time; /* end time of the page; < 0 for header pages */
	int bos;
} raw_page;

static raw_page *queue;
static size_t q_head, q_len, q_cap;

static void queue_push(const ogg_page *og, double t)
{
	raw_page *p;
	if (q_len == q_cap) {
		if (q_head > 0) {
			memmove(queue, queue + q_head, (q_len - q_head) * sizeof(raw_page));
			q_len -= q_head;
			q_head = 0;
		}
		if (q_len == q_cap) {
			q_cap = q_cap ? q_cap * 2 : 256;
			queue = realloc(queue, q_cap * sizeof(raw_page));
			if (queue == NULL)
				die("out of memory%s", "");
		}
	}
	p = &queue[q_len++];
	p->len = og->header_len + og->body_len;
	p->data = xmalloc((size_t)p->len);
	memcpy(p->data, og->header, og->header_len);
	memcpy(p->data + og->header_len, og->body, og->body_len);
	p->time = t;
	p->bos = ogg_page_bos((ogg_page *)og);
}

static FILE *out_f;

static void write_bytes(const void *p, size_t n)
{
	if (fwrite(p, 1, n, out_f) != n)
		die("write failed: %s", strerror(errno));
}

/* Writes queued pages up to time t, in arrival order; only BOS pages if
 * bos_only. */
static void queue_flush(double t, int bos_only)
{
	while (q_head < q_len) {
		raw_page *p = &queue[q_head];
		if (bos_only ? !p->bos : p->time > t)
			break;
		write_bytes(p->data, (size_t)p->len);
		free(p->data);
		q_head++;
	}
}

/* -- streams -------------------------------------------------------------- */

#define MAX_STREAMS 16

typedef struct {
	int serial;
	int drop;  /* not audio or video (e.g. Ogg Skeleton): left out */
	long rate; /* Vorbis sample rate */
	double last_time;
} copied_stream;

static copied_stream copied[MAX_STREAMS];
static int n_copied;

static copied_stream *find_copied(int serial)
{
	int i;
	for (i = 0; i < n_copied; i++)
		if (copied[i].serial == serial)
			return &copied[i];
	return NULL;
}

static double copied_page_time(copied_stream *s, ogg_page *og)
{
	ogg_int64_t gp = ogg_page_granulepos(og);
	if (gp > 0 && s->rate > 0)
		s->last_time = (double)gp / s->rate;
	return s->last_time;
}

/* -- input: headers -------------------------------------------------------- */

typedef struct {
	FILE *f;
	ogg_sync_state oy;
	ogg_stream_state to;
	int serial;
	th_info ti;
	th_comment tc;
	th_setup_info *ts;
	th_dec_ctx *dec;
} input;

/* Reads up to the first video data packet. keep_others queues the pages of
 * the other streams for copying; otherwise they are dropped. Returns NULL on
 * success, or why the file cannot be handled. */
static const char *open_input(input *in, const char *path, int keep_others)
{
	ogg_page og;
	ogg_packet op;
	int headers = 0;

	memset(in, 0, sizeof(*in));
	in->serial = -1;
	in->f = fopen(path, "rb");
	if (in->f == NULL)
		die("cannot open %s", path);
	ogg_sync_init(&in->oy);
	th_info_init(&in->ti);
	th_comment_init(&in->tc);

	while (headers < 3) {
		int serial;
		if (!read_page(in->f, &in->oy, &og))
			return in->serial < 0 ? "no Theora stream" : "truncated Theora headers";
		serial = ogg_page_serialno(&og);
		if (ogg_page_bos(&og)) {
			ogg_stream_state test;
			int is_theora = 0;
			ogg_stream_init(&test, serial);
			ogg_stream_pagein(&test, &og);
			if (ogg_stream_packetpeek(&test, &op) == 1) {
				if (op.bytes >= 7 && memcmp(op.packet, "\x80theora", 7) == 0) {
					if (in->serial >= 0)
						return "more than one Theora stream";
					if (th_decode_headerin(&in->ti, &in->tc, &in->ts, &op) < 0)
						return "bad Theora header";
					ogg_stream_packetout(&test, &op);
					in->to = test;
					in->serial = serial;
					headers = 1;
					is_theora = 1;
				} else {
					/* Theorafile only reads Theora and Vorbis streams. */
					int vorbis = op.bytes >= 16 && memcmp(op.packet, "\x01vorbis", 7) == 0;
					if (n_copied == MAX_STREAMS)
						return "too many streams";
					copied[n_copied].serial = serial;
					copied[n_copied].drop = !vorbis;
					copied[n_copied].rate = !vorbis ? 0 :
						(long)op.packet[12] | (long)op.packet[13] << 8 |
						(long)op.packet[14] << 16 | (long)op.packet[15] << 24;
					copied[n_copied].last_time = -1;
					n_copied++;
					if (keep_others && vorbis)
						queue_push(&og, -1);
				}
			}
			if (!is_theora)
				ogg_stream_clear(&test);
			continue;
		}
		if (serial == in->serial) {
			ogg_stream_pagein(&in->to, &og);
			while (headers < 3) {
				int r = ogg_stream_packetout(&in->to, &op);
				if (r == 0)
					break;
				if (r < 0 || th_decode_headerin(&in->ti, &in->tc, &in->ts, &op) <= 0)
					return "bad Theora header";
				headers++;
			}
		} else if (keep_others) {
			copied_stream *s = find_copied(serial);
			if (s == NULL)
				return "page of an unknown stream";
			if (!s->drop)
				queue_push(&og, copied_page_time(s, &og));
		}
	}
	if (in->ti.pixel_fmt == TH_PF_RSVD)
		return "reserved pixel format";
	in->dec = th_decode_alloc(&in->ti, in->ts);
	if (in->dec == NULL)
		return "cannot set up the decoder";
	return NULL;
}

static void close_input(input *in)
{
	if (in->dec)
		th_decode_free(in->dec);
	if (in->ts)
		th_setup_free(in->ts);
	th_comment_clear(&in->tc);
	th_info_clear(&in->ti);
	if (in->serial >= 0)
		ogg_stream_clear(&in->to);
	ogg_sync_clear(&in->oy);
	fclose(in->f);
}

static double info_fps(const th_info *ti)
{
	return ti->fps_denominator ? (double)ti->fps_numerator / ti->fps_denominator : 0;
}

static const char *fmt_name(int pf)
{
	return pf == TH_PF_420 ? "420" : pf == TH_PF_422 ? "422" : pf == TH_PF_444 ? "444" : "?";
}

/* Decodes the rest of the video; returns the number of frames, duplicates
 * included. */
static long decode_all(input *in, timing *t)
{
	ogg_page og;
	ogg_packet op;
	ogg_int64_t gp;
	th_ycbcr_buffer ycbcr;
	long frames = 0;
	for (;;) {
		int r;
		while ((r = ogg_stream_packetout(&in->to, &op)) != 0) {
			double t0;
			if (r < 0)
				continue;
			t0 = now_ms();
			r = th_decode_packetin(in->dec, &op, &gp);
			if (r == 0) {
				th_decode_ycbcr_out(in->dec, ycbcr);
				timing_add(t, now_ms() - t0);
				frames++;
			} else if (r == TH_DUPFRAME) {
				frames++;
			}
		}
		if (!read_page(in->f, &in->oy, &og))
			break;
		if (ogg_page_serialno(&og) == in->serial)
			ogg_stream_pagein(&in->to, &og);
	}
	return frames;
}

/* -- shrinking ---------------------------------------------------------- */

typedef struct {
	th_enc_ctx *enc;
	ogg_stream_state os;
	int kf_force;
	double last_time;
	unsigned char *buf[2]; /* current and pending frame */
	th_ycbcr_buffer ycbcr[2];
	int pending, dups, have_pending;
} encoder;

static void write_video_page(encoder *e, ogg_page *og)
{
	ogg_int64_t gp = ogg_page_granulepos(og);
	if (gp >= 0)
		e->last_time = th_granule_time(e->enc, gp);
	queue_flush(e->last_time, 0);
	write_bytes(og->header, (size_t)og->header_len);
	write_bytes(og->body, (size_t)og->body_len);
}

static void encode_one(encoder *e, th_ycbcr_buffer img, int dups, int last)
{
	ogg_packet op;
	ogg_page og;
	if (dups > 0 && th_encode_ctl(e->enc, TH_ENCCTL_SET_DUP_COUNT, &dups, sizeof(dups)) != 0)
		die("cannot encode duplicate frames%s", "");
	if (th_encode_ycbcr_in(e->enc, img) != 0)
		die("encoder refused a frame%s", "");
	while (th_encode_packetout(e->enc, last, &op) > 0) {
		ogg_stream_packetin(&e->os, &op);
		while (ogg_stream_pageout(&e->os, &og))
			write_video_page(e, &og);
	}
}

/* A run of duplicates may not span a keyframe interval. */
static void encode_frame(encoder *e, th_ycbcr_buffer img, int dups, int last)
{
	while (dups >= e->kf_force) {
		encode_one(e, img, e->kf_force - 1, 0);
		dups -= e->kf_force;
	}
	encode_one(e, img, dups, last);
}

static void fit_size(int pw, int ph, int mw, int mh, int *ow, int *oh)
{
	double s = (double)mw / pw;
	if ((double)mh / ph < s)
		s = (double)mh / ph;
	if (s > 1)
		s = 1;
	*ow = ((int)floor(pw * s + 0.5)) & ~1;
	*oh = ((int)floor(ph * s + 0.5)) & ~1;
	if (*ow < 16)
		*ow = 16;
	if (*oh < 16)
		*oh = 16;
}

int main(int argc, char **argv)
{
	int max_w = 640, max_h = 480, quality = 44, speed = 3, verify = 1, bench = 0;
	double min_shrink = 0.9;
	const char *in_path = NULL, *out_path = NULL, *why;
	input in;
	encoder e;
	th_info eti;
	scaler sc[3];
	timing src_t = {0}, out_t = {0};
	ogg_page og;
	ogg_packet op;
	long frames = 0, out_frames;
	int ow, oh, pli, hdec, vdec, i;
	double t_start = now_ms();
	ogg_uint32_t kf;

	for (i = 1; i < argc; i++) {
		const char *a = argv[i];
		if (!strcmp(a, "--max") && i + 1 < argc) {
			if (sscanf(argv[++i], "%dx%d", &max_w, &max_h) != 2 || max_w < 16 || max_h < 16)
				die("bad --max %s", argv[i]);
		} else if (!strcmp(a, "--quality") && i + 1 < argc) {
			quality = atoi(argv[++i]);
		} else if (!strcmp(a, "--speed") && i + 1 < argc) {
			speed = atoi(argv[++i]);
		} else if (!strcmp(a, "--min-shrink") && i + 1 < argc) {
			min_shrink = atof(argv[++i]);
		} else if (!strcmp(a, "--no-verify")) {
			verify = 0;
		} else if (!strcmp(a, "--bench")) {
			bench = 1;
		} else if (a[0] == '-' && a[1] == '-') {
			die("unknown option %s", a);
		} else if (in_path == NULL) {
			in_path = a;
		} else if (out_path == NULL) {
			out_path = a;
		} else {
			die("too many arguments%s", "");
		}
	}
	if (in_path == NULL || (out_path == NULL && !bench)) {
		fprintf(stderr, "usage: ogvshrink [--max WxH] [--quality Q] [--speed S] [--min-shrink F]"
		                " [--no-verify] IN OUT\n       ogvshrink --bench IN\n");
		return 1;
	}
	if (quality < 0 || quality > 63)
		die("--quality must be 0-63%s", "");

	why = open_input(&in, in_path, !bench);
	if (why != NULL) {
		printf("skip=%s\n", why);
		return EXIT_SKIP;
	}
	printf("src_size=%ux%u\n", in.ti.pic_width, in.ti.pic_height);
	printf("fps=%.3f\n", info_fps(&in.ti));
	printf("pixel_fmt=%s\n", fmt_name(in.ti.pixel_fmt));

	if (bench) {
		frames = decode_all(&in, &src_t);
		printf("frames=%ld\n", frames);
		timing_print("src", &src_t, info_fps(&in.ti));
		close_input(&in);
		return 0;
	}

	fit_size((int)in.ti.pic_width, (int)in.ti.pic_height, max_w, max_h, &ow, &oh);
	if (ow >= in.ti.pic_width * min_shrink) {
		printf("skip=already fits %dx%d\n", max_w, max_h);
		close_input(&in);
		return EXIT_SKIP;
	}

	th_info_init(&eti);
	eti.frame_width = (ow + 15) & ~15;
	eti.frame_height = (oh + 15) & ~15;
	eti.pic_width = ow;
	eti.pic_height = oh;
	eti.pic_x = 0;
	eti.pic_y = 0;
	eti.colorspace = in.ti.colorspace;
	eti.pixel_fmt = in.ti.pixel_fmt;
	eti.fps_numerator = in.ti.fps_numerator;
	eti.fps_denominator = in.ti.fps_denominator;
	eti.aspect_numerator = in.ti.aspect_numerator;
	eti.aspect_denominator = in.ti.aspect_denominator;
	eti.target_bitrate = 0;
	eti.quality = quality;
	eti.keyframe_granule_shift = in.ti.keyframe_granule_shift;

	memset(&e, 0, sizeof(e));
	e.enc = th_encode_alloc(&eti);
	if (e.enc == NULL)
		die("cannot set up the encoder%s", "");
	kf = 1u << (in.ti.keyframe_granule_shift < 8 ? in.ti.keyframe_granule_shift : 8);
	th_encode_ctl(e.enc, TH_ENCCTL_SET_KEYFRAME_FREQUENCY_FORCE, &kf, sizeof(kf));
	e.kf_force = (int)kf;
	th_encode_ctl(e.enc, TH_ENCCTL_SET_SPLEVEL, &speed, sizeof(speed));
	e.last_time = -1;
	ogg_stream_init(&e.os, in.serial);

	hdec = !(in.ti.pixel_fmt & 1);
	vdec = !(in.ti.pixel_fmt & 2);
	for (i = 0; i < 2; i++) {
		size_t ysz = (size_t)eti.frame_width * eti.frame_height;
		size_t csz = (size_t)(eti.frame_width >> hdec) * (eti.frame_height >> vdec);
		e.buf[i] = xmalloc(ysz + 2 * csz);
		for (pli = 0; pli < 3; pli++) {
			e.ycbcr[i][pli].width = pli ? (int)eti.frame_width >> hdec : (int)eti.frame_width;
			e.ycbcr[i][pli].height = pli ? (int)eti.frame_height >> vdec : (int)eti.frame_height;
			e.ycbcr[i][pli].stride = e.ycbcr[i][pli].width;
			e.ycbcr[i][pli].data = e.buf[i] + (pli == 0 ? 0 : ysz + (pli - 1) * csz);
		}
	}
	scaler_init(&sc[0], (int)in.ti.pic_x, (int)in.ti.pic_y, (int)in.ti.pic_width,
	            (int)in.ti.pic_height, ow, oh);
	for (pli = 1; pli < 3; pli++) {
		int cx0 = (int)in.ti.pic_x >> hdec, cy0 = (int)in.ti.pic_y >> vdec;
		int cx1 = (int)(in.ti.pic_x + in.ti.pic_width + hdec) >> hdec;
		int cy1 = (int)(in.ti.pic_y + in.ti.pic_height + vdec) >> vdec;
		scaler_init(&sc[pli], cx0, cy0, cx1 - cx0, cy1 - cy0, ow >> hdec, oh >> vdec);
	}

	out_f = fopen(out_path, "wb");
	if (out_f == NULL)
		die("cannot create %s", out_path);

	/* Headers: our BOS page first, the other streams' BOS pages, then the
	 * rest of the Theora headers and the other streams' header pages. */
	if (th_encode_flushheader(e.enc, &in.tc, &op) <= 0)
		die("cannot write the Theora headers%s", "");
	ogg_stream_packetin(&e.os, &op);
	while (ogg_stream_flush(&e.os, &og))
		write_bytes(og.header, (size_t)og.header_len), write_bytes(og.body, (size_t)og.body_len);
	queue_flush(0, 1);
	while (th_encode_flushheader(e.enc, &in.tc, &op) > 0)
		ogg_stream_packetin(&e.os, &op);
	while (ogg_stream_flush(&e.os, &og))
		write_bytes(og.header, (size_t)og.header_len), write_bytes(og.body, (size_t)og.body_len);
	queue_flush(-0.5, 0);

	for (;;) {
		int r;
		while ((r = ogg_stream_packetout(&in.to, &op)) != 0) {
			ogg_int64_t gp;
			th_ycbcr_buffer ycbcr;
			double t0;
			if (r < 0)
				continue;
			t0 = now_ms();
			r = th_decode_packetin(in.dec, &op, &gp);
			if (r == 0) {
				int cur = !e.pending;
				th_decode_ycbcr_out(in.dec, ycbcr);
				timing_add(&src_t, now_ms() - t0);
				for (pli = 0; pli < 3; pli++)
					scaler_run(&sc[pli], &ycbcr[pli], e.ycbcr[cur][pli].data,
					           e.ycbcr[cur][pli].stride);
				if (e.have_pending)
					encode_frame(&e, e.ycbcr[e.pending], e.dups, 0);
				e.pending = cur;
				e.dups = 0;
				e.have_pending = 1;
				frames++;
			} else if (r == TH_DUPFRAME && e.have_pending) {
				e.dups++;
				frames++;
			}
		}
		if (!read_page(in.f, &in.oy, &og))
			break;
		if (ogg_page_bos(&og)) {
			printf("skip=chained stream\n");
			fclose(out_f);
			remove(out_path);
			return EXIT_SKIP;
		}
		if (ogg_page_serialno(&og) == in.serial) {
			ogg_stream_pagein(&in.to, &og);
		} else {
			copied_stream *s = find_copied(ogg_page_serialno(&og));
			if (s != NULL && !s->drop)
				queue_push(&og, copied_page_time(s, &og));
		}
	}
	if (!e.have_pending)
		die("no video frames in %s", in_path);
	encode_frame(&e, e.ycbcr[e.pending], e.dups, 1);
	while (ogg_stream_flush(&e.os, &og))
		write_video_page(&e, &og);
	queue_flush(1e300, 0);
	if (fclose(out_f) != 0)
		die("write failed: %s", strerror(errno));

	printf("frames=%ld\n", frames);
	timing_print("src", &src_t, info_fps(&in.ti));
	printf("out_size=%dx%d\n", ow, oh);
	printf("quality=%d\n", quality);

	th_encode_free(e.enc);
	ogg_stream_clear(&e.os);
	for (pli = 0; pli < 3; pli++)
		scaler_free(&sc[pli]);
	free(e.buf[0]);
	free(e.buf[1]);
	close_input(&in);

	if (verify) {
		why = open_input(&in, out_path, 0);
		if (why != NULL)
			die("output does not decode: %s", why);
		out_frames = decode_all(&in, &out_t);
		if (out_frames != frames) {
			fprintf(stderr, "ogvshrink: output has %ld frames, input %ld\n", out_frames, frames);
			return 1;
		}
		timing_print("out", &out_t, info_fps(&in.ti));
		close_input(&in);
	}
	printf("elapsed_s=%.1f\n", (now_ms() - t_start) / 1000.0);
	return 0;
}
