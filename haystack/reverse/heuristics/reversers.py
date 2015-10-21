# -*- coding: utf-8 -*-

import logging
import struct
import sys
import time

import os

from haystack.abc import interfaces
from haystack.reverse import config
from haystack.reverse import structure
from haystack.reverse import fieldtypes
from haystack.reverse import utils
from haystack.reverse import pattern
from haystack.reverse.heuristics import model
from haystack.reverse.heuristics import dsa
from haystack.reverse.heuristics import pointertypes

"""
BasicCachingReverser:
    use heapwalker to organise heap user allocations chunks into raw records.

AbstractRecordReverser:
    Implement this class when you are delivering a IRecordReverser
    The reverse method will iterate on all record in a context and call reverse_record

FieldReverser:
    Decode each structure by asserting simple basic types from the byte content.
    Text, Pointers, Integers...

PointerFieldReverser:
    Identify pointer fields and their target structure.

DoubleLinkedListReverser:
    Identify double Linked list. ( list, vector, ... )

PointerGraphReverser:
    use the pointer relation between records to map a graph.

save_headers:
    Save the python class code definition to file.

reverse_instances:
        # we use common allocators to find structures.
        use DoubleLinkedListReverser to try to find some double linked lists records
        use FieldReverser to decode bytes contents to find basic types
        use PointerFieldReverser to identify pointer relation between structures
        use PointerGraphReverser to graph pointer relations between structures
        save guessed records' python code definition to file
"""

log = logging.getLogger('reversers')


class BasicCachingReverser(model.AbstractReverser):
    """
    Uses heapwalker to get user allocations into structures in cache.
    This reverser should be use as a first step in the reverse process.
    """

    REVERSE_LEVEL = 1

    def _iterate_records(self, _context):
        for x in enumerate(zip(map(long, self._allocations), map(long, _context.list_allocations_sizes()))):
            yield x

    def reverse_context(self, _context):
        log.info('[+] Reversing user allocations into cache')
        self._loaded = 0
        self._unused = 0
        # FIXME why is that a LIST ?????
        self._done_records = _context._structures.keys()
        self._allocations = _context.list_allocations_addresses()
        #
        self._todo = sorted(set(self._allocations) - set(self._done_records))
        self._fromcache = len(self._allocations) - len(self._todo)
        log.info('[+] Adding new raw structures from getUserAllocations cached contents - %d todo', len(self._todo))
        super(BasicCachingReverser, self).reverse_context(_context)

    def reverse_record(self, _context, _record):
        i, (ptr_value, size) = _record
        if ptr_value in self._done_records:
            sys.stdout.write('.')
            sys.stdout.flush()
            return
        self._loaded += 1
        if size < 0:
            log.error("Negative allocation size")
        mystruct = structure.AnonymousRecord(_context.memory_handler, ptr_value, size)
        _context._structures[ptr_value] = mystruct
        # cache to disk
        mystruct.saveme(_context)
        return


class DoubleLinkedListReverser(model.AbstractReverser):
    """
    Identify double Linked list. ( list, vector, ... )

    All allocation in the list must have the same size.
    All pointer field should be at the same offset.

      FIXME: make a "KnownRecordTyepReverser"
      That can apply on the full allocated chunk or a subsets of fields.

        Use a LIST_ENTRY in that reverser to replace this.

            class LIST_ENTRY(ctypes.Structure):
                _fields_ = [('Next', ctypes.POINTER(LIST_ENTRY)),
                            ('Back', ctypes.POINTER(LIST_ENTRY))]

        we also need advanced constraints in the search API to be able to check for next_back == current ...
    """

    REVERSE_LEVEL = 30

    def __init__(self, _memory_handler):
        super(DoubleLinkedListReverser, self).__init__(_memory_handler)
        self.found = 0
        self.members = set()
        self.lists = {}

    def _is_record_address_in_lists(self, address, field_offset, record_size):
        # there could be multiple list of record of same length,
        # with list entry fields at the same offset
        # NOT extend
        if record_size in self.lists:
            if field_offset in self.lists[record_size]:
                for members in self.lists[record_size][field_offset]:
                    if address in members:
                        return True
        return False

    def _add_new_list(self, field_offset, record_size, list_items):
        if record_size not in self.lists:
            self.lists[record_size] = {}
        if field_offset not in self.lists[record_size]:
            self.lists[record_size][field_offset] = []
        # there could be multiple list of record of same length,
        # with list entry fields at the same offset
        # NOT extend
        self.lists[record_size][field_offset].append(list_items)
        return

    def reverse_record(self, _context, _record):
        """
        Check if we find a LIST_ENTRY construct basically at every field.
        Returns fast if _record's reverse level is over this one.
        """
        log.debug('heap is %s', _context.heap)
        # FIXME, we should check any field offset where a,b is a couple of pointer to the same type
        if _record.get_reverse_level() >= self.get_reverse_level():
            # ignore this record. its already reversed.
            self._nb_from_cache += 1
        else:
            # we will at least only try around valid pointerfields.
            for _field in _record.get_fields()[:-1]:
                if _field.is_pointer():
                    self.reverse_field(_context, _record, _field)
        self._nb_reversed += 1
        return

    def reverse_field(self, _context, _record, _field):
        """
        Check if we find a LIST_ENTRY construct basically at this field/word + the next one.
        Returns fast if this field's is already part of a list.
        """
        offset = _field.offset
        ptr_value = _field.offset + _record.address
        size = len(_record)
        # check if the ptr is a known member at this offset
        if self._is_record_address_in_lists(_record.address, offset, len(_record)):
            self._nb_from_cache += 1
        elif self.is_linked_list_member(_context, ptr_value, offset, size):
            # _members will contain record's address for this offset, back and next.
            head_addr, _members = self.iterate_list(_context, ptr_value, offset, size)
            if _members is not None:
                self._add_new_list(offset, len(_record), _members)
                self._nb_reversed += len(_members)
                # change the type and fields for the whole list of record
                self._rename_and_split(_context, _members, offset, head_addr)
                self.found += 1
                log.debug('0x%x is a linked_list_member in a list of %d members', head_addr, len(_members))
            else:
                log.debug('Iterate_list returned no list members')
        else:
            log.debug('0x%x is not a linked_list_member', ptr_value)

    def is_linked_list_member(self, _context, ptr_value, offset, size):
        """
        Checks if this address hold a DoubleLinkedPointer record with forward and backward pointers.
        with b=ptr_value-offset, pointers are valid for a->b<-c

        Check that _next and _back are valid record in heap
        :param ptr_value:
        :return:
        """
        _next, _back = self.get_two_pointers(_context, ptr_value)
        if (_next == ptr_value) or (_back == ptr_value):
            # this are self pointers that could be a list head or end
            log.debug('Either f1(%s) or f2(%s) points to self', _next == ptr_value, _back == ptr_value)
            return False
        tn = _context.is_known_address(_next-offset)
        tb = _context.is_known_address(_back-offset)
        if not (tn and tb):
            # at least one pointer value is dangling.
            log.debug('Either Next(%s) or Back(%s) ptr are not records in heap', tn, tb)
            return False
        # classic LIST_ENTRY
        # log.debug('Next and Back are pointing to known records fields')
        # get next and prev in the same HEAP
        _next_next, _next_back = self.get_two_pointers(_context, _next)
        _back_next, _back_back = self.get_two_pointers(_context, _back)
        # check if the three pointer work
        cbn = (ptr_value == _next_back)
        cnb = (ptr_value == _back_next)
        if not (cbn and cnb):
            log.debug('ptr->next->previous not met on cbn(%s) or cnb(%s)', cbn, cnb)
            return False
        ## checking the size of the items
        ## FIXME replace by the size list
        if len(_context.get_record_for_address(_next-offset)) != size:
            log.debug('ptr->next size != %s', size)
            return False
        if len(_context.get_record_for_address(_back-offset)) != size:
            log.debug('ptr->back size != %s', size)
            return False
        return True

    def get_two_pointers(self, _context, st_addr, offset=0):
        """
        Read two words from an address as to get 2 pointers out.
        usually that is what a double linked list structure is.
        """
        # TODO add PEP violation fmt ignore. get_word_type_char returns a str()
        fmt = str(self._target.get_word_type_char()*2)
        m = _context.memory_handler.get_mapping_for_address(st_addr + offset)
        _bytes = m.read_bytes(st_addr + offset, 2 * self._target.get_word_size())
        return struct.unpack(fmt, _bytes)

    def iterate_list(self, _context, _address, offset, size):
        """
        Iterate the list starting at _address.

        Given list: a <-> b <-> c <-> d
        _address is either b or c
        We will return a,b,c,d

        :param _address:
        :return:
        """
        # FIXME, we are missing a and d
        if not self.is_linked_list_member(_context, _address, offset, size):
            return None, None
        ends = []
        members = [_address-offset]
        _next, _back = self.get_two_pointers(_context, _address)
        current = _address
        # check that  a->_address<->_next<-c are part of the list
        while self.is_linked_list_member(_context, _next, offset, size):
            if _next-offset in members:
                log.debug('loop from 0x%x to member 0x%x', current-offset, _next-offset)
                break
            members.append(_next-offset)
            _next, _ = self.get_two_pointers(_context, _next)
            current = _next
        # we found an end
        ends.append((current, 'Next', _next))
        if _next-offset not in members:
            members.append(_next-offset)

        # now the other side
        current = _address
        while self.is_linked_list_member(_context, _back, offset, size):
            if _back-offset in members:
                log.debug('loop from 0x%x to member 0x%x', current-offset, _back-offset)
                break
            members.insert(0, _back-offset)
            _, _back = self.get_two_pointers(_context, _back)
            current = _back
        # we found an end
        ends.append((current, 'Back', _back))
        if _back-offset not in members:
            members.insert(0, _back-offset)

        log.debug('head:0x%x members:%d tail:0x%x', current, len(members), ends[0][0])
        #for m in members:
        #    print hex(m), '->',
        #print
        return current-offset, members

    def _rename_and_split(self, _context, _members, offset, head_addr):
        """
        Change the type of the 2 pointers to a substructure.
        Rename the field to reflect this .
        Rename the _record ?

        :param _context:
        :param _members:
        :param offset:
        :param head_addr:
        :return:
        """
        # use member[1] instead of head, so that we have a better chance for field types.
        # in head, back pointer is probably a zero value, not a pointer field type.
        _record = _context.get_record_for_address(_members[1])
        # we need two pointer fields to create a substructure.
        ## Check if field at offset is a pointer, If so change it name, otherwise split
        old_next = _record.get_field_at_offset(offset)
        old_back = _record.get_field_at_offset(offset+self._word_size)
        #
        next_field = fieldtypes.PointerField(0, self._word_size)
        back_field = fieldtypes.PointerField(self._word_size, self._word_size)
        next_field.set_name('Next')
        back_field.set_name('Back')
        sub_fields = [next_field, back_field]
        # make a substructure
        new_field = fieldtypes.RecordField(_record, offset, 'list', 'LIST_ENTRY', sub_fields)
        fields = [x for x in _record.get_fields()]
        fields.remove(old_next)
        if old_next == old_back:
            # its probably a LIST_ENTRY btw.
            return
        fields.remove(old_back)
        fields.append(new_field)
        fields.sort()

        # create a new type
        head_addr = _members[0]
        _record_type = structure.RecordType('list_%x' % head_addr, len(_record), fields)

        # apply the fields template to all members of the list
        for list_item_addr in _members:
            _item = _context.get_record_for_address(list_item_addr)
            if len(_item) != len(_record):
                print "x2 linked reverser: len(_item) != len(_record)"
            else:
                _item.set_type(_record_type)

        # push the LIST_ENTRY type into the context/memory_handler
        rev_context = self._memory_handler.get_reverse_context()
        rev_context.add_reversed_type(_record_type, _members)

        # change the list_head name back
        _context.get_record_for_address(head_addr).set_name('list_head')
        pass

    def _split_and_set_pointer_fieldtype(self, _record, offset):
        # split the old field into 2
        # set the newly created to pointer type.
        # reduce the size of the previous old field
        old_field = _record.get_field_at_offset(offset)
        old_start_offset = old_field.offset
        old_end_offset = old_start_offset + len(old_field)
        assert old_start_offset < offset < old_end_offset
        assert offset % self._word_size == 0
        field = fieldtypes.PointerField(offset, self._word_size)
        # we do not care about the pointer value as we will create a record type and
        # reload the instance with that type.
        # check
        # tail insertion case
        if field.offset+len(field) == old_end_offset:
            # reduce old_field
            old_field.resize()
        return


class PointerGraphReverser(model.AbstractReverser):
    """
      use the pointer relation between structure to map a graph.
    """
    REVERSE_LEVEL = 150

    def __init__(self, _memory_handler):
        super(PointerGraphReverser, self).__init__(_memory_handler)
        import networkx
        self._master_graph = networkx.DiGraph()
        self._heaps_graph = networkx.DiGraph()
        self._graph = None

    def reverse(self):
        super(PointerGraphReverser, self).reverse()
        import networkx
        dumpname = self._memory_handler.get_name()
        outname1 = os.path.sep.join([config.get_cache_folder_name(dumpname), config.CACHE_GRAPH])
        outname2 = os.path.sep.join([config.get_cache_folder_name(dumpname), config.CACHE_GRAPH_HEAP])

        log.info('[+] Process Graph == %d Nodes', self._master_graph.number_of_nodes())
        log.info('[+] Process Graph == %d Edges', self._master_graph.number_of_edges())
        networkx.readwrite.gexf.write_gexf(self._master_graph, outname1)
        log.info('[+] Process Heaps Graph == %d Nodes', self._heaps_graph.number_of_nodes())
        log.info('[+] Process Heaps Graph == %d Edges', self._heaps_graph.number_of_edges())
        networkx.readwrite.gexf.write_gexf(self._heaps_graph, outname2)
        return

    def reverse_context(self, _context):
        import networkx
        # we only need the addresses...
        self._graph = networkx.DiGraph()
        log.info('[+] Heap 0x%x Graph += %d Nodes', _context._heap_start, self._graph.number_of_nodes())
        t0 = time.time()
        tl = t0
        for _record in _context.listStructures():
            # in all case
            self._graph.add_node(hex(_record.address), heap=_context._heap_start, weight=len(_record))
            self._master_graph.add_node(hex(_record.address), heap=_context._heap_start, weight=len(_record))
            self._heaps_graph.add_node(hex(_record.address), heap=_context._heap_start, weight=len(_record))
            self.reverse_record(_context, _record)
            # output headers
        #
        log.info('[+] Heap 0x%x Graph += %d Edges', _context._heap_start, self._graph.number_of_edges())
        networkx.readwrite.gexf.write_gexf(self._graph, _context.get_filename_cache_graph())
        ##
        return

    def reverse_record(self, heap_context, _record):
        ptr_value = _record.address
        # targets = set(( '%x'%ptr_value, '%x'%child.target_struct_addr )
        # for child in struct.getPointerFields()) #target_struct_addr
        # target_struct_addr

        pointer_fields = [f for f in _record.get_fields() if f.is_pointer()]
        for f in pointer_fields:
            pointee_addr = f._child_addr
            # we always feed these two
            self._graph.add_edge(hex(_record.address), hex(pointee_addr))
            self._master_graph.add_edge(hex(_record.address), hex(pointee_addr))
            # but we only feed the heaps graph if the target is known
            heap = self._memory_handler.get_mapping_for_address(pointee_addr)
            heap_context = self._memory_handler.get_reverse_context().get_context_for_heap(heap)
            if heap_context is None:
                continue
            try:
                pointee = heap_context.get_record_at_address(pointee_addr)
            except IndexError as e:
                continue
            except ValueError as e:
                continue
            self._heaps_graph.add_edge(hex(_record.address), hex(pointee_addr))
        return


class ArrayFieldsReverser(model.AbstractReverser):
    """
    Aggregate fields of similar type into arrays in the record.
    """
    REVERSE_LEVEL = 200

    def reverse_record(self, _context, _record):
        """
            Aggregate fields of similar type into arrays in the record.
        """
        if _record.get_reverse_level() < 30:
            raise ValueError('The record reverse level needs to be >30')

        log.debug('0x%x: %s', _record.address, _record.get_signature(text=True))

        _record._dirty = True

        _record._fields.sort()
        myfields = []

        signature = _record.get_signature()
        pencoder = pattern.PatternEncoder(signature, minGroupSize=3)
        patterns = pencoder.makePattern()

        #txt = self.getSignature(text=True)
        #log.warning('signature of len():%d, %s'%(len(txt),txt))
        #p = pattern.findPatternText(txt, 2, 3)
        # log.debug(p)

        #log.debug('aggregateFields came up with pattern %s'%(patterns))

        # pattern is made on FieldType,
        # so we need to dequeue self.fields at the same time to enqueue in
        # myfields
        for nb, fieldTypesAndSizes in patterns:
            # print 'fieldTypesAndSizes:',fieldTypesAndSizes
            if nb == 1:
                fieldType = fieldTypesAndSizes[0]  # its a tuple
                field = _record._fields.pop(0)
                myfields.append(field)  # single el
                #log.debug('simple field:%s '%(field) )
            # array of subtructure DEBUG XXX TODO
            elif len(fieldTypesAndSizes) > 1:
                log.debug('substructure with sig %s' % (fieldTypesAndSizes))
                myelements = []
                for i in range(nb):
                    fields = [ _record._fields.pop(0) for i in range(len(fieldTypesAndSizes))]  # nb-1 left
                    #otherFields = [ self.fields.pop(0) for i in range((nb-1)*len(fieldTypesAndSizes)) ]
                    # need global ref to compare substructure signature to
                    # other anonstructure
                    firstField = fieldtypes.RecordField(_record, fields[0].offset, 'unk', 'typename', fields)
                    myelements.append(firstField)
                array = fieldtypes.ArrayField(myelements)
                myfields.append(array)
                #log.debug('array of structure %s'%(array))
            elif len(fieldTypesAndSizes) == 1:  # make array of elements or
                log.debug("found array of %s",  _record._fields[0].typename.basename)
                fields = [_record._fields.pop(0) for i in range(nb)]
                array = fieldtypes.ArrayField(fields)
                myfields.append(array)
                #log.debug('array of elements %s'%(array))
            else:  # TODO DEBUG internal struct
                raise ValueError("fields patterns len is incorrect %d" % len(fieldTypesAndSizes))

        log.debug('done with aggregateFields')
        _record.reset()
        _record.add_fields(myfields)
        _record.set_reverse_level(self._reverse_level)
        # print 'final', self.fields
        log.debug('0x%x: %s', _record.address, _record.get_signature(text=True))
        return


class InlineRecordReverser(model.AbstractReverser):
    """
    Detect record types in a large one .
    """
    REVERSE_LEVEL = 200

    def reverse_record(self, _context, _record):
        if not _record.resolvedPointers:
            raise ValueError('I should be resolved')
        _record._dirty = True
        _record._fields.sort()
        myfields = []

        signature = _record.get_type_signature()
        pencoder = pattern.PatternEncoder(signature, minGroupSize=2)
        patterns = pencoder.makePattern()

        txt = _record.get_type_signature(text=True)
        p = pattern.findPatternText(txt, 1, 2)

        log.debug('substruct typeSig: %s' % txt)
        log.debug('substruct findPatterntext: %s' % p)
        log.debug('substruct came up with pattern %s' % patterns)

        # pattern is made on FieldType,
        # so we need to dequeue _record.fields at the same time to enqueue in
        # myfields
        for nb, fieldTypes in patterns:
            if nb == 1:
                field = _record._fields.pop(0)
                myfields.append(field)  # single el
                # log.debug('simple field:%s '%(field) )
            elif len(fieldTypes) > 1:  # array of subtructure DEBUG XXX TODO
                log.debug('fieldTypes:%s' % fieldTypes)
                log.debug('substructure with sig %s', ''.join([ft.sig[0] for ft in fieldTypes]))
                myelements = []
                for i in range(nb):
                    fields = [_record._fields.pop(0) for i in range(len(fieldTypes))]  # nb-1 left
                    # otherFields = [ _record.fields.pop(0) for i in range((nb-1)*len(fieldTypesAndSizes)) ]
                    # need global ref to compare substructure signature to
                    # other anonstructure
                    firstField = fieldtypes.RecordField(_record, fields[0].offset, 'unk', 'typename', fields)
                    myelements.append(firstField)
                array = fieldtypes.ArrayField(myelements)
                myfields.append(array)
                # log.debug('array of structure %s'%(array))
            # make array of elements obase on same base type
            elif len(fieldTypes) == 1:
                log.debug('found array of %s', _record._fields[0].typename.basename)
                fields = [_record._fields.pop(0) for i in range(nb)]
                array = fieldtypes.ArrayField(fields)
                myfields.append(array)
                # log.debug('array of elements %s'%(array))
            else:  # TODO DEBUG internal struct
                raise ValueError(
                    'fields patterns len is incorrect %d' %
                    (len(fieldTypes)))

        log.debug('done with findSubstructure')
        _record._fields = myfields
        # print 'final', _record.fields
        return


def refreshOne(context, ptr_value):
    """
    FIXME: usage unknown
    usage of mystruct.resolvePointers() indicates old code

    :param context:
    :param ptr_value:
    :return:
    """
    aligned = context.structures_addresses
    my_target = context.memory_handler.get_target_platform()

    lengths = [(aligned[i + 1] - aligned[i]) for i in range(len(aligned) - 1)]
    lengths.append(context.heap.end - aligned[-1])  # add tail
    size = lengths[aligned.index(ptr_value)]

    offsets = list(context.pointers_offsets)
    offsets, my_pointers_addrs = utils.dequeue(
        offsets, ptr_value, ptr_value + size)
    # save the ref/struct type
    mystruct = structure.AnonymousRecord(context.memory_handler, ptr_value, size)
    context.structures[ptr_value] = mystruct
    for p_addr in my_pointers_addrs:
        f = mystruct.add_field(
            p_addr,
            fieldtypes.FieldType.POINTER,
            my_target.get_word_size(),
            False)
    # resolvePointers
    mystruct.resolvePointers()
    # resolvePointers
    return mystruct


def save_headers(ctx, addrs=None):
    """
    Save the python class code definition to file.

    :param ctx:
    :param addrs:
    :return:
    """
    # structs_addrs is sorted
    log.info('[+] saving headers')
    fout = open(ctx.get_filename_cache_headers(), 'w')
    towrite = []
    if addrs is None:
        addrs = iter(ctx.listStructuresAddresses())
    #
    for vaddr in addrs:
        #anon = context._get_structures()[vaddr]
        anon = ctx.get_record_for_address(vaddr)
        towrite.append(anon.to_string())
        if len(towrite) >= 10000:
            try:
                fout.write('\n'.join(towrite))
            except UnicodeDecodeError as e:
                print 'ERROR on ', anon
            towrite = []
            fout.flush()
    fout.write('\n'.join(towrite))
    fout.close()
    return


def reverse_heap(memory_handler, heap_addr):
    """
    Reverse a specific heap.

    :param memory_handler:
    :param heap_addr:
    :return:
    """
    from haystack.reverse import context
    log.debug('[+] Loading the memory dump for HEAP 0x%x', heap_addr)
    ctx = context.get_context_for_address(memory_handler, heap_addr)
    try:
        # decode bytes contents to find basic types.
        log.debug('Reversing Fields')
        fr = dsa.FieldReverser(memory_handler)
        fr.reverse_context(ctx)

        # try to find some logical constructs.
        log.debug('Reversing DoubleLinkedListReverser')
        doublelink = DoubleLinkedListReverser(memory_handler)
        doublelink.reverse_context(ctx)

        # save to file
        save_headers(ctx)

        # etc
    except KeyboardInterrupt as e:
        # except IOError,e:
        log.warning(e)
        log.info('[+] %d structs extracted' % (ctx.get_record_count()))
        raise e
        pass
    pass
    return ctx


def reverse_instances(memory_handler):
    """
    Reverse all heaps in process from memory_handler

    :param memory_handler:
    :return:
    """
    assert isinstance(memory_handler, interfaces.IMemoryHandler)
    if True:
        # decode bytes contents to find basic types.
        log.debug('Reversing Fields')
        fr = dsa.FieldReverser(memory_handler)
        fr.reverse()
        # try to find some logical constructs.
        #log.debug('Reversing DoubleLinkedListReverser')
        #doublelink = DoubleLinkedListReverser(memory_handler)
        #doublelink.reverse()
    else:
        finder = memory_handler.get_heap_finder()
        heaps = finder.get_heap_mappings()
        for heap in heaps:
            heap_addr = heap.get_marked_heap_address()
            # reverse all fields in all records from that heap
            reverse_heap(memory_handler, heap_addr)

        # then and only then can we look at the PointerFields
        # identify pointer relation between structures
        log.debug('Reversing PointerFields')
        pfr = pointertypes.PointerFieldReverser(memory_handler)
        pfr.reverse()

        # save that
        for heap in heaps:
            ctx = memory_handler.get_reverse_context().get_context_for_heap(heap)
            ctx.save_structures()
            # save to file
            save_headers(ctx)

        # and then
        # graph pointer relations between structures
        log.debug('Reversing PointerGraph')
        ptrgraph = PointerGraphReverser(memory_handler)
        ptrgraph.reverse()

    return
