/* Link-only symbols; the executable uses the guest's real libc.so.3. */
int puts(const char *text) { return 0; }
int fflush(void *stream) { return 0; }
unsigned sleep(unsigned seconds) { return seconds; }
int usleep(unsigned int microseconds) { (void)microseconds; return -1; }
void _exit(int status) { for (;;) {} }
int read(int fd, void *buffer, unsigned int length)
{
    (void)fd; (void)buffer; (void)length; return -1;
}
int write(int fd, const void *buffer, unsigned int length)
{
    (void)fd; (void)buffer; (void)length; return -1;
}

/* Link-only imports for the read-only HMI title-cache probe. */
#ifdef IDRIVE_HMI_TITLE_CACHE_LINK
int open(const char *path, int flags, ...) { (void)path; (void)flags; return -1; }
long long lseek64(int fd, long long offset, int whence)
{
    (void)fd; (void)offset; (void)whence; return -1;
}
int printf(const char *format, ...) { (void)format; return -1; }
#endif
#if defined(IDRIVE_HMI_TITLE_CACHE_LINK) || defined(IDRIVE_V850_TRANSPORT_LINK)
int snprintf(char *buffer, unsigned int size, const char *format, ...)
{
    (void)buffer; (void)size; (void)format; return -1;
}
#endif

/* Link-only imports for the io-ipc V850 transport. */
#ifdef IDRIVE_V850_TRANSPORT_LINK
void *memcpy(void *target, const void *source, unsigned int length)
{
    (void)source; (void)length; return target;
}
void *memset(void *target, int value, unsigned int length)
{
    (void)value; (void)length; return target;
}
int pthread_create(void *thread, const void *attributes, void *(*start)(void *), void *argument)
{
    (void)thread; (void)attributes; (void)start; (void)argument; return -1;
}
int pthread_mutex_init(void *mutex, const void *attributes)
{
    (void)mutex; (void)attributes; return -1;
}
int pthread_mutex_lock(void *mutex) { (void)mutex; return -1; }
int pthread_mutex_unlock(void *mutex) { (void)mutex; return -1; }
#endif

/* Additional link-only imports for the native shared-image probe. The IFS
 * supplies the real libc.so.3 at runtime; this file only helps LLD resolve
 * the executable against the recovered QNX libraries. */
int getpid(void) { return 0; }
int shm_open(const char *name, int flags, unsigned int mode)
{
    (void)name; (void)flags; (void)mode; return -1;
}
int shm_unlink(const char *name) { (void)name; return -1; }
int shm_ctl(int fd, int flags, unsigned long long physical_address,
            unsigned long long size)
{
    (void)fd; (void)flags; (void)physical_address; (void)size; return -1;
}
int ftruncate(int fd, long long length) { (void)fd; (void)length; return -1; }
void *mmap(void *address, unsigned int length, int protection, int flags,
           int fd, long long offset)
{
    (void)address; (void)length; (void)protection; (void)flags;
    (void)fd; (void)offset; return (void *)-1;
}
int munmap(void *address, unsigned int length)
{
    (void)address; (void)length; return -1;
}
void *mmap_device_memory(void *address, unsigned int length, int protection,
                         int flags, unsigned long long physical_address)
{
    (void)address; (void)length; (void)protection; (void)flags;
    (void)physical_address; return (void *)-1;
}
int mlock(const void *address, unsigned int length)
{
    (void)address; (void)length; return -1;
}
int munlock(const void *address, unsigned int length)
{
    (void)address; (void)length; return -1;
}
int close(int fd) { (void)fd; return -1; }
int mem_offset64(const void *address, int fd, unsigned int length,
                 unsigned long long *offset, unsigned int *contiguous)
{
    (void)address; (void)fd; (void)length; (void)offset; (void)contiguous;
    return -1;
}
int *__get_errno_ptr(void)
{
    static int errno_value;
    return &errno_value;
}
char *strerror(int error_number)
{
    (void)error_number;
    return "link-only libc stub";
}
