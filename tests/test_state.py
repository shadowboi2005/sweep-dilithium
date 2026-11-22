"""A trial leaves nothing behind for the next one."""
import unittest

from support import program

from emu import EmulationError, EmulationTimeout

# cmp r0,#0; itttt ne; ldrne r3,[r1]; movne r0,#1 (x3); bx lr
CRASH = [0x2800, 0xBF1F, 0x680B, 0x2001, 0x2001, 0x2001, 0x4770]
# adds r0,#1 (x5); bx lr
ADD5 = [0x3001] * 5 + [0x4770]
# movs r4,#7; bx lr   /   mov r0,r4; bx lr
SET_R4, GET_R4 = [0x2407, 0x4770], [0x4620, 0x4770]


class StateTest(unittest.TestCase):
    def test_registers(self):
        m = program(set=SET_R4, get=GET_R4)
        m.call("set")
        self.assertEqual(m.call("get"), 0)

    def test_it_state_after_a_crash(self):
        m = program(crash=CRASH, add5=ADD5)
        with self.assertRaises(EmulationError):
            m.call("crash", [1, 0x60000000])
        self.assertEqual(m.call("add5", [0]), 5)

    def test_early_stop_is_not_a_timeout(self):
        m = program(wfi=[0xBF30, 0x4770])
        with self.assertRaises(EmulationError):
            m.call("wfi", timeout_s=5)

    def test_runaway_is_a_timeout(self):
        m = program(spin=[0xE7FE])
        with self.assertRaises(EmulationTimeout):
            m.call("spin", timeout_s=0.2)

    def test_memory(self):
        m = program(add5=ADD5)
        for addr in (0x00100000, 0x20000010, 0x203FFFF0, 0x3003FFF0):
            m.write(addr, b"dirty")
            m.restore_memory()
            self.assertEqual(m.read(addr, 5), bytes(5), hex(addr))


if __name__ == "__main__":
    unittest.main()
