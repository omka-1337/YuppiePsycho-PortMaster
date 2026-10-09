/*
 * Test helper: writes a 1280x720 Theora-only Ogg file whose frames are
 * followed by runs of duplicate-frame packets, including a run longer than
 * the keyframe interval, to check that ogvshrink keeps every frame.
 *
 *   ogvdupgen OUT
 *
 * Prints the total number of frames, duplicates included.
 * Built by tests/run_tests.sh with the same sources as ogvshrink.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ogg/ogg.h>
#include <theora/theoraenc.h>

static void put(FILE *f, ogg_page *og)
{
	fwrite(og->header, 1, (size_t)og->header_len, f);
	fwrite(og->body, 1, (size_t)og->body_len, f);
}

int main(int argc, char **argv)
{
	static const int dups[] = {0, 3, 0, 0, 70, 1, 0, 5};
	const int n = (int)(sizeof(dups) / sizeof(dups[0]));
	th_info ti;
	th_comment tc;
	th_enc_ctx *enc;
	th_ycbcr_buffer img;
	ogg_stream_state os;
	ogg_packet op;
	ogg_page og;
	unsigned char *buf;
	FILE *f;
	int i, x, y, total = 0;

	if (argc != 2 || (f = fopen(argv[1], "wb")) == NULL)
		return 1;
	th_info_init(&ti);
	ti.frame_width = 1280;
	ti.frame_height = 720;
	ti.pic_width = 1280;
	ti.pic_height = 720;
	ti.fps_numerator = 30;
	ti.fps_denominator = 1;
	ti.pixel_fmt = TH_PF_420;
	ti.quality = 32;
	ti.keyframe_granule_shift = 6;
	enc = th_encode_alloc(&ti);
	th_comment_init(&tc);
	ogg_stream_init(&os, 1234);
	while (th_encode_flushheader(enc, &tc, &op) > 0)
		ogg_stream_packetin(&os, &op);
	while (ogg_stream_flush(&os, &og))
		put(f, &og);

	buf = calloc(1, 1280 * 720 * 3 / 2);
	img[0].width = 1280; img[0].height = 720; img[0].stride = 1280; img[0].data = buf;
	img[1].width = 640; img[1].height = 360; img[1].stride = 640; img[1].data = buf + 1280 * 720;
	img[2] = img[1];
	img[2].data = img[1].data + 640 * 360;
	for (i = 0; i < n; i++) {
		for (y = 0; y < 720; y++)
			for (x = 0; x < 1280; x++)
				buf[y * 1280 + x] = (unsigned char)(x + y + i * 16);
		memset(img[1].data, 128 + i, 640 * 360 * 2);
		if (dups[i]) {
			int d = dups[i];
			/* A run may not span a keyframe interval. */
			while (d >= 64) {
				int part = 63;
				th_encode_ctl(enc, TH_ENCCTL_SET_DUP_COUNT, &part, sizeof(part));
				th_encode_ycbcr_in(enc, img);
				while (th_encode_packetout(enc, 0, &op) > 0)
					ogg_stream_packetin(&os, &op);
				d -= 64;
			}
			th_encode_ctl(enc, TH_ENCCTL_SET_DUP_COUNT, &d, sizeof(d));
		}
		th_encode_ycbcr_in(enc, img);
		while (th_encode_packetout(enc, i == n - 1, &op) > 0)
			ogg_stream_packetin(&os, &op);
		while (ogg_stream_pageout(&os, &og))
			put(f, &og);
		total += 1 + dups[i];
	}
	while (ogg_stream_flush(&os, &og))
		put(f, &og);
	fclose(f);
	printf("%d\n", total);
	return 0;
}
