/* Minimal TM-SDLP frame decoding for one fixed channel, mirrored from the SAT side
 * TO_LAB framing (reduced from io_lib tm_sdlp.c). */
#include <string.h>

#include "tm_sdlp_my.h"

/* A Space Packet header: version (3 bits, always 0) ... length field in bytes 4-5. */
#define TM_SDLP_MY_SPP_VERSION(p) ((p)[0] >> 5)
#define TM_SDLP_MY_SPP_APID(p)    ((uint16_t)((((p)[0] & 0x07) << 8) | (p)[1]))
#define TM_SDLP_MY_SPP_SIZE(p)    ((((size_t)(p)[4] << 8) | (p)[5]) + TM_SDLP_MY_SPP_MIN_LENGTH)

/* Checks the frame level fields the SAT side always writes, then hands the frame to SDLS
 * (or, in an SDLP-only build, just reads the dummy SPI).
 * anyFirstHdrPtr false: every packet must start and end in the frame (FHP 0), as
 * TM_SDLP_myExtractPacket needs; true: any valid First Header Pointer (receive stream). */
static int32_t TM_SDLP_myCheckFrame(uint8_t *pFrame, size_t frameSize, uint16_t *pSpi, bool anyFirstHdrPtr)
{
    uint16_t fhp;

    const TMTF_my_PriHdr_t *pHdr = (const TMTF_my_PriHdr_t *)pFrame;

    /* The SAT side always sends the whole fixed size frame. */
    if (frameSize != TM_SDLP_MY_FRAME_SIZE)
    {
        return TM_SDLP_MY_INVALID_LENGTH;
    }

    /* Channel IDs are not checked because the prototype has only one channel. */
    // if ((TMTF_MY_RD_TF_VERSION(*pHdr) != TM_SDLP_MY_DUMMY_TFVN) || (TMTF_MY_RD_SCID(*pHdr) != TM_SDLP_MY_DUMMY_SCID) ||
    //     (TMTF_MY_RD_VCID(*pHdr) != TM_SDLP_MY_DUMMY_VCID))
    // {
    //     return TM_SDLP_MY_INVALID_CHANNEL;
    // }

    /* Every frame uses the packet service. The First Header Pointer is 0 for a frame
     * read on its own, otherwise inside the data field or one of the two special values. */
    fhp = (uint16_t)TMTF_MY_RD_FIRSTHDRPTR(*pHdr);
    if (TMTF_MY_RD_SEGLENGTHID(*pHdr) != TMTF_MY_SEGLENGTHID_PKT)
    {
        return TM_SDLP_MY_INVALID_HEADER;
    }
    if (anyFirstHdrPtr ? ((fhp >= TM_SDLP_MY_DATA_LENGTH) && (fhp != TM_SDLP_MY_NO_FIRST_HDR_PTR) &&
                          (fhp != TM_SDLP_MY_IDLE_ONLY_FHP))
                       : (fhp != 0))
    {
        return TM_SDLP_MY_INVALID_HEADER;
    }

#ifdef SDLS_MY_SDLP_ONLY
    /* SDLP-only build: no Security Header and no Trailer, so there is nothing to check
     * and no SPI in the frame; report 0. */
    *pSpi = 0;
#else
    /* SDLS: SPI names an Operational TM SA; a session frame must be a new ARSN within the
     * window and pass the MAC, and is decrypted in place. GVCID is not matched against
     * the SA (dummy channel IDs). */
    if (SDLS_myProcessSecurityTm(pFrame, frameSize, TMTF_MY_PRIHDR_LENGTH, pSpi) != SA_MY_SUCCESS)
    {
        return TM_SDLP_MY_SECURITY_ERROR;
    }
#endif

    return TM_SDLP_MY_SUCCESS;
}

int32_t TM_SDLP_myExtractPacket(void *pFrame, size_t frameSize, size_t *pCursor, size_t *pPacketOffset,
                                uint16_t *pSpi)
{
    uint8_t       *pBytes = (uint8_t *)pFrame;
    uint16_t       spi;
    size_t         offset;
    size_t         remaining;
    size_t         packetSize;
    uint16_t       apid;
    int32_t        status;

    if ((pFrame == NULL) || (pCursor == NULL) || (pPacketOffset == NULL))
    {
        return TM_SDLP_MY_INVALID_ARGUMENT;
    }

    /* Cursor 0 starts a new frame: check the header once and jump to the data field. */
    if (*pCursor == 0)
    {
        status = TM_SDLP_myCheckFrame(pBytes, frameSize, &spi, false);
        if (status != TM_SDLP_MY_SUCCESS)
        {
            return status;
        }
        if (pSpi != NULL)
        {
            *pSpi = spi;
        }
        *pCursor = TM_SDLP_MY_DATA_OFFSET;
    }
    else if (frameSize != TM_SDLP_MY_FRAME_SIZE)
    {
        return TM_SDLP_MY_INVALID_LENGTH;
    }

    offset = *pCursor;
    if ((offset < TM_SDLP_MY_DATA_OFFSET) || (offset > TM_SDLP_MY_DATA_END))
    {
        return TM_SDLP_MY_INVALID_ARGUMENT;
    }

    /* The data field is full: no more packets. */
    if (offset == TM_SDLP_MY_DATA_END)
    {
        return 0;
    }

    /* The SAT side leaves either 0 bytes or room for a whole idle packet,
     * so a remainder smaller than one packet means the frame is broken. */
    remaining = TM_SDLP_MY_DATA_END - offset;
    if (remaining < TM_SDLP_MY_SPP_MIN_LENGTH)
    {
        return TM_SDLP_MY_INVALID_PACKET;
    }

    apid       = (uint16_t)(((pBytes[offset] & 0x07) << 8) | pBytes[offset + 1]);
    packetSize = (((size_t)pBytes[offset + 4] << 8) | pBytes[offset + 5]) + TM_SDLP_MY_SPP_MIN_LENGTH;

    if (packetSize > remaining)
    {
        return TM_SDLP_MY_INVALID_PACKET;
    }

    /* The idle packet always fills the rest of the data field. */
    if (apid == TM_SDLP_MY_IDLE_APID)
    {
        *pCursor = TM_SDLP_MY_DATA_END;
        return 0;
    }

    *pPacketOffset = offset;
    *pCursor       = offset + packetSize;

    return (int32_t)packetSize;
}

/* ---------------------------------------------------------------- receive stream */

size_t TM_SDLP_myRxSize(void)
{
    return sizeof(TM_SDLP_myRx_t);
}

void TM_SDLP_myRxInit(TM_SDLP_myRx_t *pRx)
{
    if (pRx != NULL)
    {
        pRx->pFrame         = NULL;
        pRx->cursor         = TM_SDLP_MY_DATA_END;
        pRx->firstHdr       = TM_SDLP_MY_DATA_END;
        pRx->fhp            = TM_SDLP_MY_NO_FIRST_HDR_PTR;
        pRx->haveMcCount    = false;
        pRx->lastMcCount    = 0;
        pRx->joining        = false;
        pRx->have           = 0;
        pRx->need           = 0;
        pRx->lostFrames     = 0;
        pRx->droppedPackets = 0;
    }
}

/* Gives up the packet being joined (a part of it is lost). */
static void TM_SDLP_myRxDropJoin(TM_SDLP_myRx_t *pRx)
{
    if (pRx->joining)
    {
        pRx->joining = false;
        pRx->droppedPackets++;
    }
    pRx->have = 0;
    pRx->need = 0;
}

int32_t TM_SDLP_myRxPutFrame(TM_SDLP_myRx_t *pRx, void *pFrame, size_t frameSize, uint16_t *pSpi)
{
    uint8_t                *pBytes = (uint8_t *)pFrame;
    const TMTF_my_PriHdr_t *pHdr   = (const TMTF_my_PriHdr_t *)pFrame;
    uint16_t                spi;
    uint8_t                 mcCount;
    int32_t                 status;

    if ((pRx == NULL) || (pFrame == NULL))
    {
        return TM_SDLP_MY_INVALID_ARGUMENT;
    }

    status = TM_SDLP_myCheckFrame(pBytes, frameSize, &spi, true);
    if (status != TM_SDLP_MY_SUCCESS)
    {
        return status;
    }
    if (pSpi != NULL)
    {
        *pSpi = spi;
    }

    /* The count goes up by one per frame (mod 256); anything else means frames were lost
     * (or one came twice), so the packet being joined cannot be completed. */
    mcCount = (uint8_t)TMTF_MY_RD_MCFRMCNT(*pHdr);
    if (pRx->haveMcCount && (mcCount != (uint8_t)(pRx->lastMcCount + 1)))
    {
        if (mcCount != pRx->lastMcCount)
        {
            pRx->lostFrames += (uint8_t)(mcCount - pRx->lastMcCount - 1);
        }
        TM_SDLP_myRxDropJoin(pRx);
    }
    pRx->haveMcCount = true;
    pRx->lastMcCount = mcCount;

    pRx->pFrame = pBytes;
    pRx->fhp    = (uint16_t)TMTF_MY_RD_FIRSTHDRPTR(*pHdr);
    if ((pRx->fhp == TM_SDLP_MY_NO_FIRST_HDR_PTR) || (pRx->fhp == TM_SDLP_MY_IDLE_ONLY_FHP))
    {
        pRx->firstHdr = TM_SDLP_MY_DATA_END;
    }
    else
    {
        pRx->firstHdr = TM_SDLP_MY_DATA_OFFSET + pRx->fhp;
    }

    /* An idle-only frame cannot carry the rest of a packet. */
    if (pRx->fhp == TM_SDLP_MY_IDLE_ONLY_FHP)
    {
        TM_SDLP_myRxDropJoin(pRx);
        pRx->cursor = TM_SDLP_MY_DATA_END;
    }
    else
    {
        /* Joining: the rest of the packet starts at the data field. Not joining: the bytes
         * before the first header belong to a packet whose start was missed. */
        pRx->cursor = pRx->joining ? TM_SDLP_MY_DATA_OFFSET : pRx->firstHdr;
    }

    return TM_SDLP_MY_SUCCESS;
}

/* Copies up to `limit` (absolute frame offset) into the packet being joined.
 * Returns true once the whole packet is in joinBuf. */
static bool TM_SDLP_myRxJoin(TM_SDLP_myRx_t *pRx, size_t limit)
{
    size_t take;

    /* The header may itself be split: complete it first to learn the packet size. */
    if (pRx->need == 0)
    {
        take = TM_SDLP_MY_SPP_HDR_LENGTH - pRx->have;
        if (take > limit - pRx->cursor)
        {
            take = limit - pRx->cursor;
        }
        memcpy(pRx->joinBuf + pRx->have, pRx->pFrame + pRx->cursor, take);
        pRx->have += take;
        pRx->cursor += take;
        if (pRx->have < TM_SDLP_MY_SPP_HDR_LENGTH)
        {
            return false;
        }
        pRx->need = TM_SDLP_MY_SPP_SIZE(pRx->joinBuf);
    }

    take = pRx->need - pRx->have;
    if (take > limit - pRx->cursor)
    {
        take = limit - pRx->cursor;
    }
    memcpy(pRx->joinBuf + pRx->have, pRx->pFrame + pRx->cursor, take);
    pRx->have += take;
    pRx->cursor += take;

    return pRx->have == pRx->need;
}

int32_t TM_SDLP_myRxNextPacket(TM_SDLP_myRx_t *pRx, const uint8_t **ppPacket)
{
    const uint8_t *pPacket;
    size_t         remaining;
    size_t         packetSize;
    size_t         limit;

    if ((pRx == NULL) || (ppPacket == NULL))
    {
        return TM_SDLP_MY_INVALID_ARGUMENT;
    }
    if (pRx->pFrame == NULL)
    {
        return TM_SDLP_MY_NO_FRAME;
    }

    while (pRx->cursor < TM_SDLP_MY_DATA_END)
    {
        /* 1. The rest of a packet from earlier frames. It may use bytes only up to the
         *    first packet that starts in this frame. */
        if (pRx->joining)
        {
            limit = pRx->firstHdr;
            if (TM_SDLP_myRxJoin(pRx, limit))
            {
                pRx->joining = false;
                packetSize   = pRx->need;
                pRx->have    = 0;
                pRx->need    = 0;

                /* It must end exactly where the next packet starts (the frame end when no
                 * packet starts in this frame). */
                if (pRx->cursor != pRx->firstHdr)
                {
                    pRx->droppedPackets++;
                    pRx->cursor = pRx->firstHdr;
                    continue;
                }
                if ((TM_SDLP_MY_SPP_VERSION(pRx->joinBuf) != 0) ||
                    (TM_SDLP_MY_SPP_APID(pRx->joinBuf) == TM_SDLP_MY_IDLE_APID))
                {
                    if (TM_SDLP_MY_SPP_VERSION(pRx->joinBuf) != 0)
                    {
                        pRx->droppedPackets++;
                    }
                    continue;
                }
                *ppPacket = pRx->joinBuf;
                return (int32_t)packetSize;
            }

            /* Not complete yet. With no packet starting here it goes on in the next frame;
             * otherwise the frame says it ended earlier than its length says: drop it. */
            if (pRx->fhp == TM_SDLP_MY_NO_FIRST_HDR_PTR)
            {
                return 0;
            }
            TM_SDLP_myRxDropJoin(pRx);
            pRx->cursor = pRx->firstHdr;
            continue;
        }

        /* 2. A packet that starts here. */
        pPacket   = pRx->pFrame + pRx->cursor;
        remaining = TM_SDLP_MY_DATA_END - pRx->cursor;

        if (remaining < TM_SDLP_MY_SPP_HDR_LENGTH)
        {
            /* Even the header runs on into the next frame. */
            pRx->joining = true;
            pRx->have    = 0;
            pRx->need    = 0;
            (void)TM_SDLP_myRxJoin(pRx, TM_SDLP_MY_DATA_END);
            return 0;
        }

        if (TM_SDLP_MY_SPP_VERSION(pPacket) != 0)
        {
            /* Not a packet header: lost track. Read again from the next frame's FHP. */
            pRx->droppedPackets++;
            pRx->cursor = TM_SDLP_MY_DATA_END;
            return 0;
        }

        packetSize = TM_SDLP_MY_SPP_SIZE(pPacket);
        if (packetSize > remaining)
        {
            pRx->joining = true;
            pRx->have    = 0;
            pRx->need    = 0;
            (void)TM_SDLP_myRxJoin(pRx, TM_SDLP_MY_DATA_END);
            return 0;
        }

        pRx->cursor += packetSize;
        if (TM_SDLP_MY_SPP_APID(pPacket) == TM_SDLP_MY_IDLE_APID)
        {
            continue;
        }
        *ppPacket = pPacket;
        return (int32_t)packetSize;
    }

    return 0;
}

void TM_SDLP_myRxGetStats(const TM_SDLP_myRx_t *pRx, uint32_t *pLostFrames, uint32_t *pDroppedPackets)
{
    if (pRx == NULL)
    {
        return;
    }
    if (pLostFrames != NULL)
    {
        *pLostFrames = pRx->lostFrames;
    }
    if (pDroppedPackets != NULL)
    {
        *pDroppedPackets = pRx->droppedPackets;
    }
}
