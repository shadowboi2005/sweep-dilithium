/* MPS2-AN386 layout as ucpqc models it: everything (code, data, bss, heap) in
   ZBT_SRAM1 at 0x0, the stack at the top of ZBT_SRAM2.  VMA == LMA so the
   loader's segment copy initialises .data; no runtime copy is needed. */
MEMORY {
  FLASH (rwx) : ORIGIN = 0x00000000, LENGTH = 4M
  SRAM  (rwx) : ORIGIN = 0x20000000, LENGTH = 4M
}
ENTRY(Reset_Handler)
SECTIONS {
  .vectors : { KEEP(*(.vectors)) } > FLASH
  .text    : { *(.text*) *(.glue_7*) *(.vfp11_veneer) *(.v4_bx) } > FLASH
  .rodata  : { *(.rodata*) } > FLASH
  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } > FLASH
  .ARM.exidx : { __exidx_start = .; *(.ARM.exidx*) __exidx_end = .; } > FLASH
  .data    : { *(.data*) } > FLASH
  .bss (NOLOAD) : { __bss_start__ = .; *(.bss*) *(COMMON) __bss_end__ = .; } > FLASH
  . = ALIGN(8);
  end = .; _end = .; __end__ = .;
  __stack_top = ORIGIN(SRAM) + LENGTH(SRAM);
}
