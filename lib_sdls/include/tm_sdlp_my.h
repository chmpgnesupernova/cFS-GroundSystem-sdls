/* Minimal TM-SDLP frame decoding for one fixed channel, mirrored from the SAT side
 * TO_LAB TM_SDLP_myAddPacketToFrame / TM_SDLP_myFillAndSendFrame (reduced from io_lib
 * tm_sdlp.h).
 * The SAT side writes packets back to back into fixed size frames. A packet that does not
 * fit is continued in the next frame (its rest goes to an overflow queue); the First
 * Header Pointer (FHP) gives the first packet that starts in each frame, or
 * TM_SDLP_MY_NO_FIRST_HDR_PTR when a frame holds only the middle of one packet. A frame
 * sent before it is full is padded with an idle packet.
 *
 * Two ways to read frames on the ground:
 *   TM_SDLP_myRx*            a receive stream: joins packets across frames (use this)
 *   TM_SDLP_myExtractPacket  one frame on its own; only for frames whose packets all start
 *                            and end inside it (FHP 0), as the earlier SAT side sent them */
#ifndef _tm_sdlp_my_h_
#define _tm_sdlp_my_h_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sdls/sdls_my.h"
#include "sdls/sechdr_my.h"
#include "tf/tmtf_my.h"

#define TM_SDLP_MY_SUCCESS          (0)
#define TM_SDLP_MY_INVALID_ARGUMENT (-2)
#define TM_SDLP_MY_INVALID_LENGTH   (-3)
#define TM_SDLP_MY_INVALID_PACKET   (-4)
#define TM_SDLP_MY_INVALID_HEADER   (-5)
#define TM_SDLP_MY_NO_FRAME         (-7) /* TM_SDLP_myRxNextPacket before a frame was put */
#define TM_SDLP_MY_SECURITY_ERROR   (-6) /* refused by SDLS (SPI, SA state, replay, MAC) */

/* Fixed dummy channel IDs written into the premade frame header by the SAT side. */
#define TM_SDLP_MY_DUMMY_TFVN 0
#define TM_SDLP_MY_DUMMY_SCID 0x003
#define TM_SDLP_MY_DUMMY_VCID 1

#define TM_SDLP_MY_FRAME_SIZE 1786

#ifdef SDLS_MY_SDLP_ONLY
/* SDLP-only: no Security Header and no Trailer. The packets start right after the
 * Primary Header and the data field runs to the frame end, as the SDLP-only satellite
 * TO frames it (to_lab TM_SDLP_MY_SDLS_HDR_LENGTH 0, TRL_LENGTH 0). */
#define TM_SDLP_MY_SDLS_HDR_LENGTH 0
#define TM_SDLP_MY_SDLS_TRL_LENGTH 0
#else
/* SDLS: Security Header right after the Primary Header (SPI 2 + IV 12 bytes) and the
 * Security Trailer (16 byte MAC) at the frame end. The same layout for every SA. */
#define TM_SDLP_MY_SDLS_HDR_LENGTH SECHDR_MY_LENGTH
#define TM_SDLP_MY_SDLS_TRL_LENGTH SDLS_MY_TRAILER_LENGTH
#endif

/* Data field position inside the frame. */
#define TM_SDLP_MY_DATA_OFFSET (TMTF_MY_PRIHDR_LENGTH + TM_SDLP_MY_SDLS_HDR_LENGTH)
#define TM_SDLP_MY_DATA_END    (TM_SDLP_MY_FRAME_SIZE - TM_SDLP_MY_SDLS_TRL_LENGTH)
#define TM_SDLP_MY_DATA_LENGTH (TM_SDLP_MY_DATA_END - TM_SDLP_MY_DATA_OFFSET)

/* First Header Pointer values with a special meaning (CCSDS 132.0-B). */
#define TM_SDLP_MY_NO_FIRST_HDR_PTR 0x7FF /* no packet starts in this frame */
#define TM_SDLP_MY_IDLE_ONLY_FHP    0x7FE /* the frame holds only idle data */

/* A Space Packet needs a 6 byte header and at least 1 data byte. */
#define TM_SDLP_MY_SPP_HDR_LENGTH 6
#define TM_SDLP_MY_SPP_MIN_LENGTH 7

/* An idle packet needs a 6 byte header and at least 1 data byte. */
#define TM_SDLP_MY_IDLE_MIN_LENGTH 7
#define TM_SDLP_MY_IDLE_APID       0x7FF

/* Largest Space Packet: 16 bit length field + 7. */
#define TM_SDLP_MY_MAX_PACKET_SIZE (0xFFFF + TM_SDLP_MY_SPP_MIN_LENGTH)

/* Receive stream: the state kept between frames of one sender. Treat as opaque. */
typedef struct
{
    /* current frame */
    uint8_t *pFrame;   /* frame given to TM_SDLP_myRxPutFrame, NULL before the first */
    size_t   cursor;   /* next unread byte of its data field */
    size_t   firstHdr; /* where the first packet starting in this frame begins (or DATA_END) */
    uint16_t fhp;      /* the frame's First Header Pointer as received */
    /* frame order */
    bool    haveMcCount;
    uint8_t lastMcCount;
    /* packet being joined across frames */
    bool   joining;
    size_t have; /* bytes of it already in joinBuf */
    size_t need; /* its total size, 0 while its 6 byte header is still incomplete */
    /* counters, since TM_SDLP_myRxInit */
    uint32_t lostFrames;     /* frames missing from the Master Channel Frame Count */
    uint32_t droppedPackets; /* packets dropped because a part of them was lost */
    uint8_t  joinBuf[TM_SDLP_MY_MAX_PACKET_SIZE];
} TM_SDLP_myRx_t;

/* SAT side code */

/* Builds the premade frame. Call once during application init. */
//int32 TM_SDLP_myInit(void);
/* Adds one packet to the frame and sends every frame that gets completed.
 * Returns a negative value on a send error; an oversized packet is dropped. */
//int32 TM_SDLP_myForwardPacket(const void *pPacket, size_t packetSize, osal_id_t sockId, const OS_SockAddr_t *pAddr);
/* Fills the current frame with idle data and sends it. An empty frame is not sent. */
//int32 TM_SDLP_mySendFrame(osal_id_t sockId, const OS_SockAddr_t *pAddr);

/* GROUND side code */

/* Finds the next Space Packet in one received TM frame (offset iteration).
 * pFrame        : received frame, in a WRITABLE buffer: on the cursor 0 call a session
 *                 frame (SA11) is MAC checked and decrypted in place
 * frameSize     : size of pFrame in bytes (must be TM_SDLP_MY_FRAME_SIZE)
 * pCursor       : in/out. Set to 0 before the first call for a frame; the Primary
 *                 Header and SDLS (SDLS_myProcessSecurityTm) are processed on that call.
 *                 Advanced past the returned packet.
 * pPacketOffset : out. Offset of the returned packet from the frame start.
 * pSpi          : out, may be NULL. Written on the cursor 0 call: the SA the frame came
 *                 on. SA10 packets are unauthenticated EP PDUs and must go to the key
 *                 exchange handler only; SA11 packets are protected telemetry.
 * Returns the packet size (> 0), 0 when the frame has no more packets
 * (the idle packet or the data field end was reached), or a negative error code.
 *
 * SDLP-only build (SDLP_myGetMode): no SA DB and no SDLS; the frame has no Security
 * Header or Trailer and *pSpi is set to 0. Everything below is the SDLS build.
 * Needs SA_myOpen (sa/sa_shm_my.h) in this process first. The SPI must name an
 * Operational TM SA: SA10 (EP PDUs, IV and MAC ignored) or SA11 (session: the IV must be
 * a new ARSN within the window and the MAC must verify). The ARSN is recorded only after
 * the MAC verifies, so the same frame passed again with cursor 0 is refused as a replay.
 * TM_SDLP_MY_SECURITY_ERROR: SDLS refused the frame (SPI, SA state, replay, MAC), or the
 * SA DB is not open. A frame that fails the MAC is left with its data field zeroized.
 *
 * Python (ctypes) usage:
 *   buf = ctypes.create_string_buffer(frame, len(frame))
 *   cursor, off, spi = c_size_t(0), c_size_t(0), c_uint16(0)
 *   while (n := lib.TM_SDLP_myExtractPacket(buf, len(frame), byref(cursor), byref(off), byref(spi))) > 0:
 *       packet = buf.raw[off.value : off.value + n]                                     */
int32_t TM_SDLP_myExtractPacket(void *pFrame, size_t frameSize, size_t *pCursor, size_t *pPacketOffset,
                                uint16_t *pSpi);

/* Receive stream (one per sender): frames in, whole Space Packets out, joined across
 * frames with the First Header Pointer.
 *
 *   TM_SDLP_myRxInit(&rx);
 *   for each received frame:
 *       if (TM_SDLP_myRxPutFrame(&rx, frame, size, &spi) == TM_SDLP_MY_SUCCESS)
 *           while ((n = TM_SDLP_myRxNextPacket(&rx, &pPacket)) > 0)
 *               use pPacket[0 .. n-1]
 *
 * A returned packet points into the frame (when it lies inside it) or into the stream
 * (when it was joined); it stays valid until the next call on the stream. Idle packets are
 * never returned.
 *
 * Lost frames are found with the Master Channel Frame Count: a packet that was being
 * joined is then dropped, and reading starts again at the next frame's First Header
 * Pointer. A packet header that is not a version 0 header, or a joined packet that does
 * not end where the First Header Pointer says the next one starts, is treated the same
 * way. Packets taken out before such a point are kept. */

/* Size of TM_SDLP_myRx_t, for callers that allocate it themselves (ctypes). */
size_t TM_SDLP_myRxSize(void);

/* Starts (or restarts) a stream: no frame, nothing being joined, counters 0. */
void TM_SDLP_myRxInit(TM_SDLP_myRx_t *pRx);

/* Checks one received frame and makes it the stream's current frame.
 * pFrame    : received frame, in a WRITABLE buffer that stays valid until the next
 *             TM_SDLP_myRxPutFrame (SDLS build: decrypted in place, as for
 *             TM_SDLP_myExtractPacket; packets returned later point into it)
 * frameSize : must be TM_SDLP_MY_FRAME_SIZE
 * pSpi      : out, may be NULL: the SA the frame came on (0 in an SDLP-only build)
 * Returns TM_SDLP_MY_SUCCESS, or the same frame errors as TM_SDLP_myExtractPacket
 * (any First Header Pointer is accepted that points into the data field or is one of
 * TM_SDLP_MY_NO_FIRST_HDR_PTR / TM_SDLP_MY_IDLE_ONLY_FHP). A refused frame changes
 * nothing in the stream; its absence is found from the next frame's count. */
int32_t TM_SDLP_myRxPutFrame(TM_SDLP_myRx_t *pRx, void *pFrame, size_t frameSize, uint16_t *pSpi);

/* The next whole Space Packet of the current frame.
 * ppPacket : out: the packet
 * Returns its size (> 0), 0 when the frame has no more whole packets (the rest of a
 * packet that runs on is kept for the next frame), TM_SDLP_MY_NO_FRAME before the first
 * frame, or TM_SDLP_MY_INVALID_ARGUMENT. */
int32_t TM_SDLP_myRxNextPacket(TM_SDLP_myRx_t *pRx, const uint8_t **ppPacket);

/* Counters since TM_SDLP_myRxInit. Any pointer may be NULL. */
void TM_SDLP_myRxGetStats(const TM_SDLP_myRx_t *pRx, uint32_t *pLostFrames, uint32_t *pDroppedPackets);

#endif /* _tm_sdlp_my_h_ */
