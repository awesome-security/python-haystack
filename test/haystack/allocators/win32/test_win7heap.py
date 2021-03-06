#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tests for haystack.reverse.structure."""

import logging
import unittest
import sys

from haystack import dump_loader
from haystack.allocators.win32 import win7heapwalker

from test.testfiles import putty_1_win7

log = logging.getLogger('testwin7heap')


class TestWin7Heap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.memory_handler = dump_loader.load(putty_1_win7.dumpname)
        return

    @classmethod
    def tearDownClass(cls):
        cls.memory_handler.reset_mappings()
        cls.memory_handler = None
        return

    def test_ctypes_sizes(self):
        """putty.1.dump is a win7 32 bits memory dump"""
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x00390000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        my_ctypes = self.memory_handler.get_target_platform().get_target_ctypes()
        my_utils = self.memory_handler.get_target_platform().get_target_ctypes_utils()

        self.assertEqual(my_ctypes.sizeof(win7heap.HEAP_SEGMENT), 64)
        self.assertEqual(my_ctypes.sizeof(win7heap.HEAP_ENTRY), 8)
        self.assertEqual(my_ctypes.sizeof(my_ctypes.POINTER(None)), 4)
        self.assertEqual(my_ctypes.sizeof(
            my_ctypes.POINTER(win7heap.HEAP_TAG_ENTRY)), 4)
        self.assertEqual(my_ctypes.sizeof(win7heap.LIST_ENTRY), 8)
        self.assertEqual(my_ctypes.sizeof(
            my_ctypes.POINTER(win7heap.HEAP_LIST_LOOKUP)), 4)
        self.assertEqual(my_ctypes.sizeof(
            my_ctypes.POINTER(win7heap.HEAP_PSEUDO_TAG_ENTRY)), 4)
        self.assertEqual(my_ctypes.sizeof(my_ctypes.POINTER(win7heap.HEAP_LOCK)), 4)
        self.assertEqual(my_ctypes.sizeof(my_ctypes.c_ubyte), 1)
        self.assertEqual(my_ctypes.sizeof((my_ctypes.c_ubyte * 1)), 1)
        self.assertEqual(my_ctypes.sizeof(win7heap.HEAP_COUNTERS), 84)
        self.assertEqual(my_ctypes.sizeof(win7heap.HEAP_TUNING_PARAMETERS), 8)

        self.assertEqual(my_ctypes.sizeof(win7heap.HEAP), 312)
        self.assertEqual(my_utils.offsetof(win7heap.HEAP, 'Signature'), 100)

    def test_is_heap(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        my_ctypes = self.memory_handler.get_target_platform().get_target_ctypes()
        h = self.memory_handler.get_mapping_for_address(0x005c0000)
        self.assertEqual(h.get_byte_buffer()[0:10],
                          b'\xc7\xf52\xbc\xc9\xaa\x00\x01\xee\xff')
        addr = h.start
        self.assertEqual(addr, 6029312)
        heap = walker.get_heap()
        # heap = h.read_struct(addr, win7heap.HEAP)

        # check that haystack memory_mapping works
        self.assertEqual(my_ctypes.addressof(h._local_mmap_content),
                          my_ctypes.addressof(heap))
        # check heap.Signature
        self.assertEqual(heap.Signature, 4009750271)  # 0xeeffeeff
        # we need to initialize the heaps for _is_heap
        ## mappings = finder.get_heap_mappings()
        # a load_member by validator occurs in heapwalker._is_heap

    def test_is_heap_all(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        for addr, size in putty_1_win7.known_heaps:
            h = self.memory_handler.get_mapping_for_address(addr)
            walker = finder.get_heap_walker(h)
            heap = walker.get_heap()
            # check heap.Signature
            self.assertEqual(heap.Signature, 4009750271)  # 0xeeffeeff

        finder = self.memory_handler.get_heap_finder()
        heaps = sorted([(h.get_heap_address(), len(h.get_heap_mapping())) for h in finder.list_heap_walkers()])
        self.assertEqual(heaps, putty_1_win7.known_heaps)
        self.assertEqual(len(heaps), len(putty_1_win7.known_heaps))

    def test_get_UCR_segment_list(self):
        # You have to import after ctypes has been tuned ( mapping loader )
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        ucrs = validator.HEAP_get_UCRanges_list(heap)
        self.assertEqual(heap.UCRIndex.value, 0x5c0590)
        self.assertEqual(heap.Counters.TotalUCRs, 1)
        # in this example, there is one UCR in 1 segment.
        self.assertEqual(len(ucrs), heap.Counters.TotalUCRs)
        ucr = ucrs[0]
        # UCR will point to non-mapped space. But reserved address space.
        self.assertEqual(ucr.Address.value, 0x6b1000)
        self.assertEqual(ucr.Size, 0xf000)  # bytes
        self.assertEqual(ucr.Address.value + ucr.Size, 0x6c0000)
        # check numbers.
        reserved_size = heap.Counters.TotalMemoryReserved
        committed_size = heap.Counters.TotalMemoryCommitted
        ucr_size = reserved_size - committed_size
        self.assertEqual(ucr.Size, ucr_size)

    def test_HEAP_get_UCRanges_list(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        # get an example
        for heap_addr, ucr_list in putty_1_win7.known_ucr.items():
            # get the heap
            h = self.memory_handler.get_mapping_for_address(heap_addr)
            walker = finder.get_heap_walker(h)
            validator = walker.get_heap_validator()
            heap = walker.get_heap()
            # get UCRList from heap
            # TODO TotalUCRs == Total UCRS from UCRSegments. Not from Heap UCRList
            reserved_ucrs = validator.HEAP_get_UCRanges_list(heap)
            if len(reserved_ucrs) != heap.Counters.TotalUCRs:
                _tmp_ucrs = validator.collect_all_ucrs(heap)
                self.assertEqual(len(_tmp_ucrs), heap.Counters.TotalUCRs) # , "Bad count for heap 0x%x" % heap_addr)
            self.assertEqual(len(ucr_list), heap.Counters.TotalUCRs)
            # check numbers.
            reserved_size = heap.Counters.TotalMemoryReserved
            committed_size = heap.Counters.TotalMemoryCommitted
            ucr_size = reserved_size - committed_size
            self.assertEqual(ucr_size, ucr_size)
            # TODO: what is a LargeUCR
            ucr_total_size = sum([ucr.Size for ucr in reserved_ucrs if ucr.Size >= 512*1024])
            self.assertEqual(ucr_total_size, heap.Counters.TotalMemoryLargeUCR)



    def test_get_segment_list(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        self.assertEqual(h, mapping)
        walker = finder.get_heap_walker(h)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        segments = validator.get_segment_list(heap)
        self.assertEqual(heap.Counters.TotalSegments, 1)
        self.assertEqual(len(segments), heap.Counters.TotalSegments)
        segment = segments[0]
        self.assertEqual(segment.SegmentSignature, 0xffeeffee)
        self.assertEqual(segment.FirstEntry.value, 0x5c0588)
        self.assertEqual(segment.LastValidEntry.value, 0x06c0000)
        # only segment is self heap here
        self.assertEqual(segment.Heap.value, addr)
        self.assertEqual(segment.BaseAddress.value, addr)
        # checkings size. a page is 4096 in this example.
        valid_alloc_size = (heap.LastValidEntry.value
                            - heap.FirstEntry.value)
        meta_size = (heap.FirstEntry.value
                     - heap.BaseAddress.value)
        committed_size = heap.Counters.TotalMemoryCommitted
        reserved_size = heap.Counters.TotalMemoryReserved
        ucr_size = reserved_size - committed_size
        self.assertEqual(segment.NumberOfPages * 4096, reserved_size)
        self.assertEqual(segment.NumberOfPages * 4096, 0x100000)  # example
        self.assertEqual(reserved_size, meta_size + valid_alloc_size)

    def test_get_segment_list_all(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        for addr, size in putty_1_win7.known_heaps:
            h = self.memory_handler.get_mapping_for_address(addr)
            walker = finder.get_heap_walker(h)
            validator = walker.get_heap_validator()
            heap = walker.get_heap()

            segments = validator.get_segment_list(heap)
            self.assertEqual(len(segments), heap.Counters.TotalSegments)
            pages = 0
            total_size = 0
            for segment in segments:
                self.assertEqual(segment.SegmentSignature, 0xffeeffee)
                self.assertEqual(validator._utils.get_pointee_address(segment.Heap), addr)
                base_addr = validator._utils.get_pointee_address(segment.BaseAddress)
                first_entry_addr = validator._utils.get_pointee_address(segment.FirstEntry)
                last_entry_addr = validator._utils.get_pointee_address(segment.LastValidEntry)
                self.assertLess(base_addr, first_entry_addr)
                self.assertLess(first_entry_addr, last_entry_addr)
                valid_alloc_size = (last_entry_addr - first_entry_addr)
                meta_size = first_entry_addr - base_addr
                pages += segment.NumberOfPages
                total_size += valid_alloc_size + meta_size
            # Heap resutls for all segments
            committed_size = heap.Counters.TotalMemoryCommitted
            reserved_size = heap.Counters.TotalMemoryReserved
            self.assertEqual(pages * 4096, reserved_size)
            self.assertEqual(total_size, reserved_size)

    def test_get_chunks(self):
        # You have to import after ctypes has been tuned ( mapping loader )
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        walker = finder.get_heap_walker(h)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        allocated, free = validator.get_backend_chunks(heap)
        s_allocated = sum([c[1] for c in allocated])
        s_free = sum([c[1] for c in free])
        total = sorted(allocated | free)
        s_total = sum([c[1] for c in total])

        # in this example, its a single continuous segment
        for i in range(len(total) - 1):
            if total[i][0] + total[i][1] != total[i + 1][0]:
                self.fail(
                    'Chunk Gap between %s %s ' %
                    (total[i],
                     total[
                        i +
                        1]))
        chunks_size = total[-1][0] + total[-1][1] - total[0][0]
        # HEAP segment was aggregated into HEAP
        valid_alloc_size = (heap.LastValidEntry.value
                            - heap.FirstEntry.value)
        meta_size = (heap.FirstEntry.value
                     - heap.BaseAddress.value)
        committed_size = heap.Counters.TotalMemoryCommitted
        reserved_size = heap.Counters.TotalMemoryReserved
        ucr_size = reserved_size - committed_size

        # 1 chunk is 8 bytes.
        self.assertEqual(s_free / 8, heap.TotalFreeSize)
        self.assertEqual(committed_size, meta_size + chunks_size)
        self.assertEqual(reserved_size, meta_size + chunks_size + ucr_size)

        # LFH bins are in some chunks, at heap.FrontEndHeap

    def test_get_chunks_all(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        for addr, size in putty_1_win7.known_heaps:
            h = self.memory_handler.get_mapping_for_address(addr)
            validator = walker.get_heap_validator()
            heap = walker.get_heap()

            # BUG is here !!!
            allocated, free = validator.get_backend_chunks(heap)
            s_allocated = sum([c[1] for c in allocated])
            s_free = sum([c[1] for c in free])
            total = sorted(allocated | free)
            s_total = sum([c[1] for c in total])
            # HEAP counters
            committed_size = heap.Counters.TotalMemoryCommitted
            reserved_size = heap.Counters.TotalMemoryReserved
            ucr_size = reserved_size - committed_size

            # in some segments, they are non-contiguous segments
            chunks_size = sum([chunk[1] for chunk in total])
            # chunks are in all segments
            alloc_size = 0
            for segment in validator.get_segment_list(heap):
                valid_alloc_size = (segment.LastValidEntry.value
                                    - segment.FirstEntry.value)
                alloc_size += valid_alloc_size
            # 1 chunk is 8 bytes.
            self.assertEqual(s_free / 8, heap.TotalFreeSize)
            # sum of allocated size for every segment should amount to the
            # sum of all allocated chunk
            self.assertEqual(alloc_size, chunks_size + ucr_size)

    def test_get_freelists(self):
        # You have to import after ctypes has been tuned ( mapping loader )
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        allocated, free = validator.get_backend_chunks(heap)
        freelists = validator.HEAP_get_freelists(heap)
        free_size = sum([x[1] for x in [(hex(x[0]), x[1]) for x in freelists]])
        free_size2 = sum([x[1] for x in free])
        self.assertEqual(heap.TotalFreeSize * 8, free_size)
        self.assertEqual(free_size, free_size2)

    def test_get_freelists_all(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        for addr, size in putty_1_win7.known_heaps:
            h = self.memory_handler.get_mapping_for_address(addr)
            validator = walker.get_heap_validator()
            heap = walker.get_heap()

            allocated, free = validator.get_backend_chunks(heap)
            freelists = validator.HEAP_get_freelists(heap)
            free_size = sum([x[1] for x in
                             [(hex(x[0]), x[1]) for x in freelists]])
            free_size2 = sum([x[1] for x in free])
            self.assertEqual(heap.TotalFreeSize * 8, free_size)
            self.assertEqual(free_size, free_size2)

    def test_get_frontend_chunks(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        fth_committed, fth_free = validator.get_frontend_chunks(heap)
        # SizeInCache : 59224L,

        # not much to check...
        lfh = h.read_struct(heap.FrontEndHeap.value, win7heap.LFH_HEAP)
        self.assertEqual(lfh.Heap.value, addr)
        # FIXME: check more.

    def test_get_vallocs(self):
        # test/dumps/keepass.live.prod 0x00410000
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        addr = 0x005c0000
        h = self.memory_handler.get_mapping_for_address(addr)
        validator = walker.get_heap_validator()
        heap = walker.get_heap()

        valloc_committed = validator.HEAP_get_virtual_allocated_blocks_list(heap)

        size = sum([x.ReserveSize for x in valloc_committed])
        # FIXME Maybe ??
        self.assertEqual(heap.Counters.TotalSizeInVirtualBlocks, size)

    def test_get_vallocs_all(self):
        finder = win7heapwalker.Win7HeapFinder(self.memory_handler)
        mapping = self.memory_handler.get_mapping_for_address(0x005c0000)
        walker = finder.get_heap_walker(mapping)
        win7heap = walker._heap_module
        for addr, size in putty_1_win7.known_heaps:
            h = self.memory_handler.get_mapping_for_address(addr)
            validator = walker.get_heap_validator()
            heap = walker.get_heap()

            valloc_committed = validator.HEAP_get_virtual_allocated_blocks_list(heap)
            size = sum([x.ReserveSize for x in valloc_committed])
            self.assertEqual(heap.Counters.TotalSizeInVirtualBlocks, size)


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    # logging.getLogger('testwin7heap').setLevel(level=logging.DEBUG)
    # logging.getLogger('win7heapwalker').setLevel(level=logging.DEBUG)
    # logging.getLogger('win7heap').setLevel(level=logging.DEBUG)
    # logging.getLogger('listmodel').setLevel(level=logging.DEBUG)
    # logging.getLogger('dump_loader').setLevel(level=logging.INFO)
    # logging.getLogger('types').setLevel(level=logging.DEBUG)
    # logging.getLogger('memory_mapping').setLevel(level=logging.INFO)
    unittest.main(verbosity=2)
    #suite = unittest.TestLoader().loadTestsFromTestCase(TestFunctions)
    # unittest.TextTestRunner(verbosity=2).run(suite)
