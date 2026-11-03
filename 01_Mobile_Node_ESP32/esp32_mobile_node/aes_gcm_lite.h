// ============================================================================
//  aes_gcm_lite.h — self-contained AES-128-GCM encryption for the ESP32 node
// ============================================================================
//  Why this exists: the ESP32 Arduino core's bundled mbedtls headers are not
//  on the include path in every core version/board package ("mbedtls/gcm.h:
//  No such file or directory"). This file implements exactly what the node
//  needs — AES-128 block encryption (FIPS-197) and GCM encryption with a
//  96-bit nonce and 128-bit tag (NIST SP 800-38D) — in plain C with no
//  dependencies, so the sketch compiles on any ESP32 core version.
//
//  It was verified byte-for-byte against the gateway's Python implementation
//  (the `cryptography` library's AESGCM) on hundreds of random keys, nonces,
//  additional-data and plaintext lengths — see TESTING_EVIDENCE.md.
//
//  Encryption only (the node never decrypts). Speed is irrelevant here: one
//  51-byte packet takes well under a millisecond on the ESP32.
// ============================================================================
#pragma once
#include <stdint.h>
#include <string.h>

static const uint8_t AESL_SBOX[256] = {
  0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
  0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
  0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
  0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
  0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
  0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
  0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
  0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
  0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
  0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
  0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
  0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
  0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
  0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
  0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
  0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
};

static inline uint8_t aesl_xtime(uint8_t x) {
  return (uint8_t)((x << 1) ^ ((x & 0x80) ? 0x1b : 0x00));
}

// AES-128 key expansion: 11 round keys of 16 bytes.
static void aesl_key_expand(const uint8_t key[16], uint8_t rk[176]) {
  static const uint8_t rcon[10] = {0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36};
  memcpy(rk, key, 16);
  for (int i = 4; i < 44; i++) {
    uint8_t t[4];
    memcpy(t, rk + 4 * (i - 1), 4);
    if (i % 4 == 0) {
      uint8_t first = t[0];
      t[0] = (uint8_t)(AESL_SBOX[t[1]] ^ rcon[i / 4 - 1]);
      t[1] = AESL_SBOX[t[2]];
      t[2] = AESL_SBOX[t[3]];
      t[3] = AESL_SBOX[first];
    }
    for (int j = 0; j < 4; j++) rk[4 * i + j] = (uint8_t)(rk[4 * (i - 4) + j] ^ t[j]);
  }
}

// Encrypt one 16-byte block (state in column-major order, as in FIPS-197).
static void aesl_encrypt_block(const uint8_t rk[176], const uint8_t in[16], uint8_t out[16]) {
  uint8_t s[16];
  for (int i = 0; i < 16; i++) s[i] = (uint8_t)(in[i] ^ rk[i]);
  for (int round = 1; round <= 10; round++) {
    uint8_t t[16];
    // SubBytes + ShiftRows
    for (int c = 0; c < 4; c++)
      for (int r = 0; r < 4; r++)
        t[4 * c + r] = AESL_SBOX[s[4 * ((c + r) % 4) + r]];
    if (round != 10) {  // MixColumns
      for (int c = 0; c < 4; c++) {
        uint8_t a0 = t[4 * c], a1 = t[4 * c + 1], a2 = t[4 * c + 2], a3 = t[4 * c + 3];
        uint8_t all = (uint8_t)(a0 ^ a1 ^ a2 ^ a3);
        t[4 * c]     = (uint8_t)(a0 ^ all ^ aesl_xtime((uint8_t)(a0 ^ a1)));
        t[4 * c + 1] = (uint8_t)(a1 ^ all ^ aesl_xtime((uint8_t)(a1 ^ a2)));
        t[4 * c + 2] = (uint8_t)(a2 ^ all ^ aesl_xtime((uint8_t)(a2 ^ a3)));
        t[4 * c + 3] = (uint8_t)(a3 ^ all ^ aesl_xtime((uint8_t)(a3 ^ a0)));
      }
    }
    for (int i = 0; i < 16; i++) s[i] = (uint8_t)(t[i] ^ rk[16 * round + i]);
  }
  memcpy(out, s, 16);
}

// Multiplication in GF(2^128) with GCM's bit ordering (SP 800-38D, Alg. 1).
static void aesl_gf_mult(const uint8_t x[16], const uint8_t y[16], uint8_t z[16]) {
  uint8_t v[16], acc[16] = {0};
  memcpy(v, y, 16);
  for (int i = 0; i < 128; i++) {
    if (x[i / 8] & (0x80 >> (i % 8))) {
      for (int j = 0; j < 16; j++) acc[j] ^= v[j];
    }
    uint8_t lsb = (uint8_t)(v[15] & 1);
    for (int j = 15; j > 0; j--) v[j] = (uint8_t)((v[j] >> 1) | (v[j - 1] << 7));
    v[0] >>= 1;
    if (lsb) v[0] ^= 0xe1;
  }
  memcpy(z, acc, 16);
}

// GHASH over data, zero-padded to a multiple of 16 bytes.
static void aesl_ghash(uint8_t y[16], const uint8_t h[16], const uint8_t *data, size_t len) {
  while (len > 0) {
    size_t n = len < 16 ? len : 16;
    for (size_t i = 0; i < n; i++) y[i] ^= data[i];
    aesl_gf_mult(y, h, y);
    data += n;
    len -= n;
  }
}

// AES-128-GCM encryption with a 12-byte nonce and a 16-byte tag.
// ciphertext may equal plaintext (in-place). Always returns 0.
static int aes128_gcm_encrypt(const uint8_t key[16], const uint8_t nonce[12],
                              const uint8_t *aad, size_t aad_len,
                              const uint8_t *plaintext, size_t len,
                              uint8_t *ciphertext, uint8_t tag[16]) {
  uint8_t rk[176], h[16] = {0}, j0[16], ctr[16], ks[16], y[16] = {0};
  aesl_key_expand(key, rk);
  aesl_encrypt_block(rk, h, h);                      // H = E(K, 0^128)

  memcpy(j0, nonce, 12);                             // J0 = nonce || 0x00000001
  j0[12] = 0; j0[13] = 0; j0[14] = 0; j0[15] = 1;

  memcpy(ctr, j0, 16);
  for (size_t off = 0; off < len; off += 16) {       // CTR mode from inc32(J0)
    for (int i = 15; i >= 12; i--) { if (++ctr[i] != 0) break; }
    aesl_encrypt_block(rk, ctr, ks);
    size_t n = (len - off) < 16 ? (len - off) : 16;
    for (size_t i = 0; i < n; i++) ciphertext[off + i] = (uint8_t)(plaintext[off + i] ^ ks[i]);
  }

  aesl_ghash(y, h, aad, aad_len);                    // S = GHASH(A, C, lengths)
  aesl_ghash(y, h, ciphertext, len);
  uint8_t lens[16] = {0};
  uint64_t abits = (uint64_t)aad_len * 8, cbits = (uint64_t)len * 8;
  for (int i = 0; i < 8; i++) {
    lens[7 - i]  = (uint8_t)(abits >> (8 * i));
    lens[15 - i] = (uint8_t)(cbits >> (8 * i));
  }
  aesl_ghash(y, h, lens, 16);

  aesl_encrypt_block(rk, j0, ks);                    // T = E(K, J0) XOR S
  for (int i = 0; i < 16; i++) tag[i] = (uint8_t)(ks[i] ^ y[i]);
  memset(rk, 0, sizeof(rk));
  return 0;
}
