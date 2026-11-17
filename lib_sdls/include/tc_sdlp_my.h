/* Minimal TC-SDLP frame encoding for one fixed channel, mirrored from the SAT side
 * TC_SDLP_myExtractPacket (based on NOS3 CI udp_tf).
 * SDLS build      : [Primary Header 5][Security Header 14][Space Packet][Security Trailer 16]
 *                   Security Header: SPI 2 + IV 12 bytes (sdls/sechdr_my.h). Security
 *                   Trailer: the MAC (sdls/sdls_my.h), zero filled on the unprotected SA00.
 * SDLP-only build : [Primary Header 5][Space Packet]
 *                   No Security Header and no Trailer, as the SDLP-only satellite CI
 *                   (ci_lab TC_SDLP_MY_SDLS_HDR_LENGTH / TRL_LENGTH 0) expects.
 * No Segment Header (Type-D frame) and no FECF. */
#ifndef _tc_sdlp_my_h_
#define _tc_sdlp_my_h_

#include <stddef.h>
#include <stdint.h>

#include "sdls/sdls_my.h"
#include "sdls/sechdr_my.h"
#include "tf/tctf_my.h"

#define TC_SDLP_MY_SUCCESS          (0)
#define TC_SDLP_MY_INVALID_ARGUMENT (-2)
#define TC_SDLP_MY_INVALID_LENGTH   (-3)
#define TC_SDLP_MY_INVALID_PACKET   (-4)
// #define TC_SDLP_MY_INVALID_CHANNEL (-5)
#define TC_SDLP_MY_SECURITY_ERROR   (-6) /* SDLS_myApplySecurityTc refused or failed for the SPI */
#define TC_SDLP_MY_BUFFER_TOO_SMALL (-7)
#define TC_SDLP_MY_NOT_INITIALIZED  (-8)

/* Channel IDs written into every frame. They match the SAT side expected IDs. */
#define TC_SDLP_MY_DUMMY_TFVN 0
#define TC_SDLP_MY_DUMMY_SCID 0x003
#define TC_SDLP_MY_DUMMY_VCID 0

/* COP-1 is not used: Type-BD frame (Bypass = 1, Control Command = 0).
 * These match the SAT side flag check. */
#define TC_SDLP_MY_BYPASS_FLG 1
#define TC_SDLP_MY_CTLCMD_FLG 0

#ifdef SDLS_MY_SDLP_ONLY
/* SDLP-only: the Space Packet follows the Primary Header directly and ends the frame. */
#define TC_SDLP_MY_SDLS_HDR_LENGTH 0
#define TC_SDLP_MY_SDLS_TRL_LENGTH 0
#else
/* SDLS Security Header right after the Primary Header (SPI 2 + IV 12 bytes) and the
 * Security Trailer (16 byte MAC) after the packet. The same layout for every SA. */
#define TC_SDLP_MY_SDLS_HDR_LENGTH SECHDR_MY_LENGTH
#define TC_SDLP_MY_SDLS_TRL_LENGTH SDLS_MY_TRAILER_LENGTH
#endif

/* The Frame Error Control Field is optional and not used. */
#define TC_SDLP_MY_FECF_LENGTH 0

/* A Space Packet needs a 6 byte header and at least 1 data byte. */
#define TC_SDLP_MY_SPP_HDR_LENGTH  6
#define TC_SDLP_MY_SPP_MIN_LENGTH  7

/* Largest Space Packet that fits into one TC frame. */
#define TC_SDLP_MY_MAX_PACKET_LENGTH                                                       \
    (TCTF_MY_MAX_FRAME_LENGTH - TCTF_MY_PRIHDR_LENGTH - TC_SDLP_MY_SDLS_HDR_LENGTH - \
     TC_SDLP_MY_SDLS_TRL_LENGTH - TC_SDLP_MY_FECF_LENGTH)

/* SAT side code */
// int32 TC_SDLP_myExtractPacket(CFE_SB_Buffer_t *pBuf, int32 frameSize);

/* GROUND side code */

/* Sets up the frame layout and resets the Frame Sequence Number.
 * Call once before TC_SDLP_myBuildFrame. Returns TC_SDLP_MY_SUCCESS. */
int32_t TC_SDLP_myInit(void);

/* Build mode, fixed at compile time (CMake SDLS_SDLP_ONLY):
 *   SDLP_MY_MODE_SDLP_ONLY : frames only, laid out like the SDLP-only satellite (ci_lab /
 *                            to_lab): TC and TM frames have no Security Header and no
 *                            Trailer, only the Primary Header and the packets. No SA DB,
 *                            no crypto library. TC_SDLP_myBuildFrame ignores spi and
 *                            TM_SDLP_myExtractPacket reports SPI 0.
 *   SDLP_MY_MODE_SDLS      : full SDLS (sdls/sdls_my.h): SA DB, AES-GCM, key exchange;
 *                            Security Header 14 B and Trailer 16 B on TC and TM.
 * The two layouts differ, so both sides must be built in the same mode. */
#define SDLP_MY_MODE_SDLP_ONLY 1
#define SDLP_MY_MODE_SDLS      2
int32_t SDLP_myGetMode(void);

/* Security Header / Trailer lengths this library was built with, so a caller (the
 * Python link) can check that it frames like the library does. Any pointer may be NULL.
 *   SDLP-only build: 0 / 0 / 0 / 0     SDLS build: 14 / 16 / 14 / 16
 * Added together with the SDLP-only layout without Security Header: a libsdls.so that
 * lacks this function is older than that and must be rebuilt. */
void SDLP_myGetLayout(uint32_t *pTcHdr, uint32_t *pTcTrl, uint32_t *pTmHdr, uint32_t *pTmTrl);

/* Wraps one Space Packet into a TC frame written to pBuf.
 * SDLS build: needs SA_myOpen (sa/sa_shm_my.h) in this process first.
 * SDLP-only build: no SA DB; the frame is the Primary Header and the packet only.
 * spi        : SA that protects the frame (ignored in an SDLP-only build). COMMON_SPI_SA00 for EP PDUs (always usable,
 *              not encrypted), COMMON_SPI_SA01 for session traffic (Operational after
 *              key exchange; AES-256-GCM, every frame takes a fresh IV).
 * pPacket    : one complete Space Packet (header + data)
 * packetSize : size of pPacket in bytes
 * pBuf       : output buffer for the frame; must not overlap pPacket
 * bufSize    : size of pBuf in bytes (TCTF_MY_MAX_FRAME_LENGTH is always enough)
 * Returns the frame size in bytes, or a negative error code.
 * TC_SDLP_MY_SECURITY_ERROR: the SA is unknown, not a TC SA, not Operational, its IVs
 * are used up, the SA DB is not open, or encryption failed (SDLS_myApplySecurityTc).
 * A refused frame consumes no IV, and the Frame Sequence Number does not advance on
 * any error. */
int32_t TC_SDLP_myBuildFrame(uint16_t spi, const void *pPacket, size_t packetSize, void *pBuf, size_t bufSize);

#endif /* _tc_sdlp_my_h_ */
