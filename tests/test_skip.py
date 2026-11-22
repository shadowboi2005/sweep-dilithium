"""The instruction skip itself."""
import unittest

from support import CODE, program, run

from emu import Skip, it_blocks

# cmp r0,#0; ite eq; moveq r1,#1; movne r1,#2; mov r0,r1; bx lr
ITE = [0x2800, 0xBF0C, 0x2101, 0x2102, 0x4608, 0x4770]
# movs r0,#0; loop: cmp r1,#0; it ne; addne r0,#1; subs r2,#1; bne loop; bx lr
LOOP = [0x2000, 0x2900, 0xBF18, 0x3001, 0x3A01, 0xD1FA, 0x4770]


class SkipTest(unittest.TestCase):
    def test_outside_it_block(self):
        self.assertEqual(run(program(t=ITE), "t", [0, 9], skip_at=8), (0, 1))

    def test_inside_it_block(self):
        m = program(t=ITE)
        self.assertEqual(run(m, "t", [0, 9], skip_at=4, it_block=(2, 8)), (9, 1))
        self.assertEqual(run(m, "t", [1, 9], skip_at=6, it_block=(2, 8)), (9, 1))

    def test_member_whose_condition_fails_is_not_a_hit(self):
        self.assertEqual(run(program(t=ITE), "t", [1, 9], skip_at=4, it_block=(2, 8)), (2, 0))

    def test_code_is_put_back(self):
        m = program(t=ITE)
        run(m, "t", [0, 9], skip_at=4, it_block=(2, 8))
        self.assertEqual(run(m, "t", [0, 9]), (1, 0))
        self.assertEqual(run(m, "t", [1, 9]), (2, 0))

    def test_only_the_first_execution(self):
        m = program(t=LOOP)
        self.assertEqual(run(m, "t", [0, 1, 3]), (3, 0))
        self.assertEqual(run(m, "t", [0, 1, 3], skip_at=6, it_block=(4, 8)), (2, 1))

    def test_block_left_by_a_branch(self):
        # movs r0,#0; L: subs r2,#1; itt ne; addne r0,#1; bne L; bx lr
        m = program(t=[0x2000, 0x3A01, 0xBF1C, 0x3001, 0xE7FB, 0x4770])
        self.assertEqual(run(m, "t", [0, 0, 5]), (4, 0))
        self.assertEqual(run(m, "t", [0, 0, 5], skip_at=6, it_block=(4, 10)), (3, 1))

    def test_size_is_read_from_clean_code(self):
        m = program(t=[0xF04F, 0x0001, 0x4770])  # mov.w r0,#1 (4 bytes); bx lr
        m.write(CODE, bytes(4))
        skip = Skip(m, CODE)
        skip.detach()
        self.assertEqual(skip.size, 4)

    def test_several_trials(self):
        m = program(t=ITE)
        for pc, block in ((CODE + 4, (CODE + 2, CODE + 8)), (CODE + 8, None)):
            skip = Skip(m, pc, block)
            for _ in range(3):
                skip.reset()
                m.restore_memory()
                m.call("t", [0, 9])
                self.assertEqual(skip.fired, 1)
                skip.disarm()
                m.restore_memory()
                self.assertEqual(m.call("t", [0, 9]), 1)
            skip.detach()

    def test_it_blocks(self):
        m = program(t=ITE)
        insns, pc = [], CODE
        while pc < CODE + 2 * len(ITE):
            insns.append(m.disasm_one(pc))
            pc += insns[-1][1]
        self.assertEqual(it_blocks(insns), dict.fromkeys((CODE + 4, CODE + 6), (CODE + 2, CODE + 8)))


if __name__ == "__main__":
    unittest.main()
