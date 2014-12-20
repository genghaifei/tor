/* Added for Tor. */

#ifndef CRYPTO_INT32_H
#define CRYPTO_INT32_H

#include "torint.h"
#define crypto_int32 int32_t
#define crypto_uint32 uint32_t

/*
 Stop signed left shifts overflowing
 by using unsigned types for 8-bit & 32-bit bitwise operations
*/

#ifndef OVERFLOW_SAFE_SIGNED_LSHIFT
#define OVERFLOW_SAFE_SIGNED_LSHIFT(s, lshift, utype, stype) \
  ((stype)((utype)(s) << (utype)(lshift)))
#endif

#include "crypto_uint32.h"

#ifdef UNSAFE_SIGNED_LSHIFT
/* the original version of the code */
#define SHL32(s, lshift) s << lshift
#else /* #ifndef UNSAFE_SIGNED_LSHIFT */
#define SHL32(s, lshift) \
  OVERFLOW_SAFE_SIGNED_LSHIFT(s, lshift, crypto_uint32, crypto_int32)
#endif /* UNSAFE_SIGNED_LSHIFT */

/* And for 8 bit types */

#define crypto_int8 int8_t
#define crypto_uint8 uint8_t

#ifdef UNSAFE_SIGNED_LSHIFT
/* the original version of the code */
#define SHL8(s, lshift) s << lshift
#else /* #ifndef UNSAFE_SIGNED_LSHIFT */
#define SHL8(s, lshift) \
  OVERFLOW_SAFE_SIGNED_LSHIFT(s, lshift, crypto_uint8, crypto_int8)
#endif /* UNSAFE_SIGNED_LSHIFT */

#endif /* CRYPTO_INT32_H */
