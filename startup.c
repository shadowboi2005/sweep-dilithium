/* Minimal vector table: ucpqc reads SP/PC from address 0 on reset.  Functions
   are called directly, so Reset_Handler only has to enable the FPU and park. */
extern char __stack_top;
void Reset_Handler(void);
__attribute__((section(".vectors"), used))
const void *vectors[2] = { &__stack_top, (void *)Reset_Handler };
void Reset_Handler(void) {
  *(volatile unsigned *)0xE000ED88 |= 0xFu << 20;   /* CPACR: CP10/CP11 full access */
  for (;;) __asm__ volatile("bkpt #0xab" :: "r"(0x18));  /* semihosting SYS_EXIT */
}
