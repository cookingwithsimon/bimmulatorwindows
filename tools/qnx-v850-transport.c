/* io-ipc transport that carries the V850 link over an emulated OMAP UART.
 *
 * Derived from IDEmu's src/v850/ioipc_idemu.c (https://github.com/herrbimmer/IDEmu).
 * Required Notice: Copyright 2026 IDEmu contributors
 * Licensed under the PolyForm Noncommercial License 1.0.0
 * (https://polyformproject.org/licenses/noncommercial/1.0.0); see
 * licenses/PolyForm-Noncommercial-1.0.0.txt. Changes for bimmulator: no
 * devc-ser driver (the UART is polled through mmap_device_memory), no TCP
 * tunnels or servicebroker helpers, configurable UART base.
 *
 * On the car, io-ipc loads ioipc-spi-omap543x.so and talks to the V850 power
 * and bus controller over SPI, DMA and GPIO lines. The emulator has none of
 * that hardware. This library exports the same init/tx/destroy entry points
 * and sends each io-ipc message to the host over a 16550 UART instead, where
 * tools/v850-sim.py plays the V850. io-ipc and its hp decoder stay stock.
 *
 * UART frame: A5 5A <type:1> <length:2 LE> <data>
 *   guest to host: 0 hello (tchannel copy), 1 tx message, 0x10 debug text
 *   host to guest: 2 rx message. Host frames end with a 16-bit byte sum.
 */
typedef unsigned int u32;
typedef unsigned char u8;
typedef unsigned short u16;
#define NULL ((void *)0)

#ifndef V850_UART_BASE
#define V850_UART_BASE 0x4806c000u /* OMAP5 UART2, QEMU serial2 */
#endif

extern void *mmap_device_memory(void *, unsigned int, int, int, unsigned long long);
extern int usleep(unsigned int);
extern int snprintf(char *, unsigned int, const char *, ...);
extern void *memcpy(void *, const void *, unsigned int);
extern void *memset(void *, int, unsigned int);
extern int pthread_create(void *, const void *, void *(*)(void *), void *);
extern int pthread_mutex_init(void *, const void *);
extern int pthread_mutex_lock(void *);
extern int pthread_mutex_unlock(void *);
/* Exported by io-ipc itself. */
extern int ioipc_bp_alloc(u32 size, void *desc);
extern int ioipc_bp_wait_alloc(u32 size, void *desc);
extern int ioipc_bp_free(void *desc);

#define PROT_READ_WRITE 0x300 /* QNX PROT_READ 0x100 | PROT_WRITE 0x200 */
#define PROT_NOCACHE 0x800
#define MAP_SHARED 1
#define UART_RBR 0x00
#define UART_THR 0x00
#define UART_IER 0x04
#define UART_FCR 0x08
#define UART_LCR 0x0c
#define UART_LSR 0x14
#define LSR_DR 0x01
#define LSR_THRE 0x20
#define MAX_FRAME 0x1000
#define RX_DESCRIPTORS 5

typedef int (*rx_callback)(void *context, void *descriptor);
typedef void (*status_callback)(void *context, u32 status);
struct transport_ops { void *f0; void *f4; rx_callback rx; void *fc; status_callback status; };

static volatile u8 *uart_;
static u8 tchannel_[0x118] __attribute__((aligned(8)));
static long long write_lock_[4] __attribute__((aligned(8)));
static u8 descriptors_[RX_DESCRIPTORS][40] __attribute__((aligned(8)));
static int next_descriptor_;
static u32 bad_frames_, frames_;

static u8 reg_read(u32 offset) { return uart_[offset]; }
static void reg_write(u32 offset, u8 value) { uart_[offset] = value; }

static int tx_room_; /* bytes the 16-byte transmit FIFO can still take */

static void put_byte(u8 value)
{
    while (!tx_room_) {
        if (reg_read(UART_LSR) & LSR_THRE) {
            tx_room_ = 16;
        } else {
            usleep(1000);
        }
    }
    reg_write(UART_THR, value);
    tx_room_--;
}

static void put_frame(int type, const void *data, int length)
{
    const u8 *bytes = data;
    int i;
    if (!uart_) {
        return;
    }
    pthread_mutex_lock(write_lock_);
    put_byte(0xa5);
    put_byte(0x5a);
    put_byte((u8)type);
    put_byte((u8)(length & 0xff));
    put_byte((u8)(length >> 8));
    for (i = 0; i < length; i++) {
        put_byte(bytes[i]);
    }
    pthread_mutex_unlock(write_lock_);
}

static void debug_text(const char *text)
{
    int length = 0;
    while (text[length]) {
        length++;
    }
    put_frame(0x10, text, length);
}

static void debugf(const char *format, u32 a, u32 b, u32 c)
{
    char text[160];
    snprintf(text, sizeof text, format, a, b, c);
    debug_text(text);
}

static u8 get_byte(void)
{
    while (!(reg_read(UART_LSR) & LSR_DR)) {
        usleep(1000);
    }
    return reg_read(UART_RBR);
}

static void deliver(const u8 *data, int length)
{
    struct transport_ops *ops = *(struct transport_ops **)(tchannel_ + 0x110);
    void *context = *(void **)(tchannel_ + 0x10c);
    void *descriptor = descriptors_[next_descriptor_];
    int status, tries;

    next_descriptor_ = (next_descriptor_ + 1) % RX_DESCRIPTORS;
    memset(descriptor, 0, 40);
    status = ioipc_bp_alloc(MAX_FRAME, descriptor);
    if (status == 12) {
        status = ioipc_bp_wait_alloc(MAX_FRAME, descriptor);
    }
    if (status) {
        debugf("v850: bp_alloc failed %u", (u32)status, 0, 0);
        return;
    }
    memcpy(*(u8 **)((u8 *)descriptor + 8), data, (unsigned int)length);
    *(u32 *)((u8 *)descriptor + 0x18) = (u32)length;
    status = ops->rx(context, descriptor);
    /* ENOMEM means io-ipc's receive queue is full. Retry so the host stalls
     * behind the UART instead of the message being dropped. */
    for (tries = 0; status == 12 && tries < 150; tries++) {
        usleep(20000);
        status = ops->rx(context, descriptor);
    }
    if (status == 12) {
        debugf("v850: receive queue full, dropped %u bytes", (u32)length, 0, 0);
        ioipc_bp_free(descriptor);
    } else if (status) {
        debugf("v850: rx callback returned %u", (u32)status, 0, 0);
    }
}

static void *receive_thread(void *argument)
{
    static u8 frame[MAX_FRAME + 2] __attribute__((aligned(8)));
    (void)argument;
    for (;;) {
        u8 type;
        u32 sum = 0;
        int length, i;
        if (get_byte() != 0xa5) {
            continue;
        }
        if (get_byte() != 0x5a) {
            continue;
        }
        type = get_byte();
        length = get_byte();
        length |= get_byte() << 8;
        if (length < 2 || length > MAX_FRAME + 2) {
            bad_frames_++;
            debugf("v850: bad frame length %u (type %x)", (u32)length, type, 0);
            continue;
        }
        for (i = 0; i < length; i++) {
            frame[i] = get_byte();
        }
        for (i = 0; i < length - 2; i++) {
            sum += frame[i];
        }
        if ((sum & 0xffff) != (u32)(frame[length - 2] | (frame[length - 1] << 8))) {
            bad_frames_++;
            debugf("v850: checksum mismatch (type %x, %u bytes, %u bad)", type, (u32)length, bad_frames_);
            continue;
        }
        if (++frames_ % 512 == 0) {
            debugf("v850: %u frames received, %u bad", frames_, bad_frames_, 0);
        }
        if (type == 2) {
            deliver(frame, length - 2);
        }
    }
    return NULL;
}

int init(void *tchannel, u32 version)
{
    u32 thread[2];
    struct transport_ops *ops;

    uart_ = mmap_device_memory(NULL, 0x1000, PROT_READ_WRITE | PROT_NOCACHE,
                               MAP_SHARED, V850_UART_BASE);
    if (uart_ == (volatile u8 *)-1) {
        uart_ = NULL;
        return 2;
    }
    reg_write(UART_IER, 0x00); /* polled: no interrupts */
    reg_write(UART_LCR, 0x03); /* 8N1 */
    reg_write(UART_FCR, 0x07); /* enable and clear both FIFOs */
    pthread_mutex_init(write_lock_, NULL);
    if (version != 0x115) {
        debugf("v850: unsupported io-ipc transport version %x", version, 0, 0);
        return 0x30;
    }
    memcpy(tchannel_, tchannel, sizeof tchannel_);
    put_frame(0, tchannel_, sizeof tchannel_);
    debugf("v850: transport ready (io-ipc %x, uart %x)", version, V850_UART_BASE, 0);
    pthread_create(thread, NULL, receive_thread, NULL);
    ops = *(struct transport_ops **)(tchannel_ + 0x110);
    ops->status(*(void **)(tchannel_ + 0x10c), 1);
    return 0;
}

int tx(void *a, void *b, void *descriptor, int c, int d, int e)
{
    u8 *data = *(u8 **)((u8 *)descriptor + 8);
    u32 length = *(u32 *)((u8 *)descriptor + 0x18);
    (void)a; (void)b; (void)c; (void)d; (void)e;
    if (length > 0 && length <= MAX_FRAME) {
        put_frame(1, data, (int)length);
    }
    ioipc_bp_free(descriptor);
    return 0;
}

int destroy(void *tchannel)
{
    (void)tchannel;
    return 0;
}
