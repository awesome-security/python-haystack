#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2011 Loic Jaquemet loic.jaquemet+python@gmail.com
#

import collections
import logging
import os
import pickle
import itertools
import numbers

from haystack.config import Config
import fieldtypes
from fieldtypes import Field, FieldType, makeArrayField
import pattern
import utils

log = logging.getLogger('structure')

DEBUG_ADDRS=[]


# a 12 Mo heap takes 30 minutes on my slow notebook
# what is \xb2 padding for ?
# huge bug with zerroes fields aggregation
# the empty space at the end of the heap is making the reverse quite slow.... logs outputs line rate 10/sec againt 2k/sec

# TODO look for VFT and malloc metadata ?
# se stdc++ to unmangle c++
# vivisect ?
# TODO 1: make an interactive thread on that anon_struct and a struct Comparator to find similar struct.
#         that is a first step towards structure identification && naming. + caching of info
#      2: dump ctypes structure into python file + cache (vaddr, Structurectypes ) to pickle file ( reloading/continue possible with less recalculation )
# create a typename for \xff * 8/16. buffer color ? array of char?

# Compare sruct type from parent with multiple pointer (

def makeStructure(context, start, size):
  return AnonymousStructInstance(context.mappings, start, context.heap.readBytes(start, size) )

class AnonymousStructInstance():
  '''
  AnonymousStruct in absolute address space.
  Comparaison between struct is done is relative addresse space.
  '''
  def __init__(self, mappings, vaddr, bytes, prefix=None):
    self.mappings = mappings
    self.vaddr = vaddr
    self.bytes = bytes
    self.fields = []
    if prefix is None:
      self.prefixname = '%lx'%(self.vaddr)
    else:
      self.prefixname = '%lx_%s'%( self.vaddr, self.prefix)
    self.resolved = False
    self.pointerResolved = False
    return
  
  def guessField(self, vaddr, typename=None, size=-1, padding=False ):
    offset = vaddr - self.vaddr
    if offset < 0 or offset > len(self):
      raise IndexError()
    if typename is None:
      typename = FieldType.UNKNOWN
    ## find the maximum size
    if size == -1:
      try: 
        nextStruct = itertools.dropwhile(lambda x: (x.offset < offset), sorted(self.fields) ).next()
        nextStructOffset = nextStruct.offset
      except StopIteration, e:
        nextStructOffset = len(self)
      maxFieldSize = nextStructOffset - offset
      size = maxFieldSize
    ##
    field = Field(self, offset, typename, size, padding)
    if typename == FieldType.UNKNOWN:
      if not field.decodeType():
        return None
    elif not field.check():
      return None
    if field.size == -1:
      raise ValueError('error here %s %s'%(field, field.typename))
    # field has been typed
    self.fields.append(field)
    self.fields.sort()
    return field

  def addField(self, vaddr, typename, size, padding ):
    offset = vaddr - self.vaddr
    return self._addField(offset, typename, size, padding)
    
  def _addField(self, offset, typename, size, padding):
    if offset < 0 or offset > len(self):
      raise IndexError()
    if typename is None:
      raise ValueError()
    # make a field with no autodecode
    field = Field(self, offset, typename, size, padding)
    # field has been typed
    self.fields.append(field)
    self.fields.sort()
    return field

  def save(self):
    self.fname = os.path.sep.join([Config.structsCacheDir, str(self)])
    pickle.dump(self, file(self.fname,'w'))
    return
  
  def _check(self,field):
    # TODO check against other fields
    return field.check()

  def decodeFields(self):
    ''' list all gaps between known fields 
        try to decode their type
            if no  pass, do not populate
            if yes add a new field
        compare the size of the gap and the size of the fiel
    '''
    # should be done by 
    #if len(self.fields) == 0: ## add a fake all-struct field
    #  self._addField(0, FieldType.UNKNOWN, size, True)
    self._fixGaps() # add padding zones
    gaps = [ f for f in self.fields if f.padding == True ] 
    sg = len(gaps)
    while  sg > 0 :
      log.debug('decode: %d gaps left'%(sg))
      # try to decode padding zone
      for field in self.fields:
        if field.decoded: # do not redecode, save
          continue
        #
        fieldType = field.decodeType()
        if fieldType is None: # we could not decode. mark it as unknown
          field.padding = False
          field.decoded = True
          continue
        # Found a new field in a padding, with a probable type...
        pass
      # reroll until completion
      self._fixGaps() 
      gaps = [ f for f in self.fields if f.padding == True ] 
      sg = len(gaps)
    #endwhile
    #aggregate zeroes fields
    self._aggregateZeroes()
    return

  def _aggregateZeroes(self):
    ''' sometimes we have a pointer in the middle of a zeroes buffer. we need to aggregate '''
    log.debug('aggregateZeroes: start')
    myfields = sorted([ f for f in self.fields if f.padding != True ])
    if len(myfields) < 2:
      log.debug('aggregateZeroes: not so much fields')
      return
    newFields = []
    newFields.append(myfields[0])
    for f in myfields[1:]:
      last = newFields[-1]
      if last.isZeroes() and f.isZeroes():
        log.debug('aggregateZeroes: field %s and %s -> %d:%d'%(last,f, last.offset,f.offset+len(f)))
        newFields[-1] = Field(self, last.offset, last.typename, len(last)+len(f), False)
      else:
        newFields.append(f)
    self.fields = newFields
    self._fixGaps()
    return
    
  def _fixGaps(self):
    ''' Fix this structure and populate empty offsets with default unknown padding fields '''
    nextoffset = 0
    self._gaps = 0
    overlaps = False
    self.fields = [ f for f in self.fields if f.padding != True ] # clean paddings to check new fields
    myfields = sorted(self.fields)
    for f in myfields:
      if f.offset > nextoffset : # add temp padding field
        self._gaps += 1
        padding = self._addField( nextoffset, FieldType.UNKNOWN, f.offset-nextoffset, True)
        log.debug('fixGaps: adding field at offset %d:%d'%(padding.offset, padding.offset+len(padding) ))
      elif f.offset < nextoffset :
        log.warning('fixGaps: overlapping fields at offset %d'%(f.offset))
        overlaps = True
      else: # == 
        pass
      nextoffset = f.offset + len(f)
    # conclude on QUEUE insertion
    if nextoffset < len(self):
      self._gaps += 1
      padding = self._addField( nextoffset, FieldType.UNKNOWN, len(self)-nextoffset, True)
      log.debug('fixGaps: adding field at queue offset %d:%d'%(padding.offset, padding.offset+len(padding) ))
    if self._gaps == 0:
      self.resolved = True
    if overlaps:
      log.debug('fixGaps: overlapping fields to fix')
      self._fixOverlaps()
    self.fields.sort()
    return
  

  def _fixOverlaps(self):
    ''' fix overlapping string fields '''
    fields = sorted([ f for f in self.fields if f.padding != True ]) # clean paddings to check new fields
    for f1, f2 in self._getOverlapping():
      log.debug('overlappings %s %s'%(f1,f2))
      f1_end = f1.offset+len(f1)
      f2_end = f2.offset+len(f2)
      if (f1.typename == f2.typename and
          f2_end == f1_end ): # same end, same type
        self.fields.remove(f2) # use the last one
        log.debug('Cleaned a  field overlaps %s %s'%(f1, f2))
      elif f1.isZeroes() and f2.isZeroes(): # aggregate
        log.debug('aggregate Zeroes')
        start = min(f1.offset,f2.offset)
        size = max(f1_end, f2_end)-start
        try:
          self.fields.remove(f1)
          self.fields.remove(f2)
          self.fields.append( Field(self, start, FieldType.ZEROES, size, False) )
        except ValueError,e:
          log.error('please bugfix')
      else: # TODO
        pass
    return
  
  def _getOverlapping(self):
    fields = sorted([ f for f in self.fields if f.padding != True ]) # clean paddings to check new fields
    lastend = 0
    oldf = None
    for f in fields:
      newend = f.offset + len(f)
      if f.offset < lastend:  ## overlaps
        yield ( oldf, f )
      oldf = f
      lastend = newend
    return
  
  def resolvePointers(self, structs_addrs, structCache):
    if self.pointerResolved:
      return
    resolved = 0
    pointerFields = self.getPointerFields()
    for field in pointerFields:
      # shorcut
      if hasattr(field, '_ptr_resolved'):
        if field._ptr_resolved:
          resolved+=1
          continue
      # if pointed is not None:  # erase previous info
      tgt = None
      if field.value in structs_addrs: 
        tgt = structCache[field.value]
        field._target_field = tgt[0]
      elif field.value in self.mappings.getHeap():
        # elif target is a STRING in the HEAP
        # set pointer type to char_p
        tgt_field = self._resolvePointerToStructField(field, structs_addrs, structCache)
        if tgt_field is not None:
          field.typename = FieldType.makePOINTER(tgt_field.typename)
          field._target_field = tgt_field
          tgt = '%s_field_%s'%(tgt_field.struct, tgt_field.getName())
        pass
      elif field.value in self.mappings: # other mappings
        tgt = 'ext_lib'
        field._ptr_to_ext_lib = True
        pass
      if tgt is not None:
        resolved+=1
        field.setName('%s_%s'%(field.typename.basename, tgt))
        field._ptr_resolved = True
    #
    if len(pointerFields) == resolved:
      if resolved != 0 :
        log.debug('%s pointers are fully resolved'%(self))
      self.pointerResolved = True
      logging.getLogger('progressive').setLevel(logging.DEBUG)
      logging.getLogger('structure').setLevel(logging.DEBUG)
      logging.getLogger('field').setLevel(logging.DEBUG)
      #self._aggregateFields()
      logging.getLogger('progressive').setLevel(logging.INFO)
      logging.getLogger('structure').setLevel(logging.INFO)
      logging.getLogger('field').setLevel(logging.INFO)
    else:
      self.pointerResolved = False
    return
  
  def _resolvePointerToStructField(self, field, structs_addrs, structCache):
    if len(structs_addrs) == 0:
      return None
    nearest_addr, ind = utils.closestFloorValue(field.value, structs_addrs)
    tgt_st = structCache[nearest_addr]
    if field.value in tgt_st:
      offset = field.value - nearest_addr
      for f in tgt_st.fields:
        if f.offset == offset:
          tgt_field = f
          return tgt_field
    return None
  
  def _aggregateFields(self):
    if not self.pointerResolved:
      raise ValueError('I should be resolved')
    
    self.fields.sort()
    myfields = []
    
    signature = self.getSignature()
    pencoder = pattern.PatternEncoder(signature, minGroupSize=3)
    patterns = pencoder.makePattern()

    txt = self.getSignature(text=True)
    log.warning('signature of len():%d, %s'%(len(txt),txt))
    p = pattern.findPatternText(txt, 2, 3)

    log.debug(p)
    #log.debug('aggregateFields came up with pattern %s'%(patterns))
    
    # pattern is made on FieldType, 
    #so we need to dequeue self.fields at the same time to enqueue in myfields
    for nb, fieldTypesAndSizes in patterns:
      #print 'fieldTypesAndSizes:',fieldTypesAndSizes
      if nb == 1:
        fieldType = fieldTypesAndSizes[0] # its a tuple
        field = self.fields.pop(0)
        myfields.append(field) # single el
        #log.debug('simple field:%s '%(field) )
      elif len(fieldTypesAndSizes) > 1: #  array of subtructure DEBUG XXX TODO
        log.debug('substructure with sig %s'%(fieldTypesAndSizes))
        myelements=[]
        for i in range(nb):
          fields = [ self.fields.pop(0) for i in range(len(fieldTypesAndSizes)) ] # nb-1 left
          #otherFields = [ self.fields.pop(0) for i in range((nb-1)*len(fieldTypesAndSizes)) ] 
          # need global ref to compare substructure signature to other anonstructure
          firstField = FieldType.makeStructField(self, fields[0].offset, fields)
          myelements.append(firstField)
        array = makeArrayField(self, myelements )
        myfields.append(array) 
        #log.debug('array of structure %s'%(array))
      elif len(fieldTypesAndSizes) == 1: #make array of elements or
        log.debug('found array of %s'%(self.fields[0].typename.basename))
        fields = [ self.fields.pop(0) for i in range(nb) ]
        array = makeArrayField(self, fields )
        myfields.append(array) 
        #log.debug('array of elements %s'%(array))
      else: # TODO DEBUG internal struct
        raise ValueError('fields patterns len is incorrect %d'%(len(fieldTypesAndSizes)))
    
    log.debug('done with aggregateFields')    
    self.fields = myfields
    #print 'final', self.fields
    return

  def _findSubStructures(self):
    if not self.pointerResolved:
      raise ValueError('I should be resolved')
    
    self.fields.sort()
    myfields = []
    
    signature = self.getTypeSignature()
    pencoder = pattern.PatternEncoder(signature, minGroupSize=2)
    patterns = pencoder.makePattern()

    txt = self.getTypeSignature(text=True)
    p = pattern.findPatternText(txt, 1, 2)

    log.debug('substruct typeSig: %s'%txt)
    log.debug('substruct findPatterntext: %s'%p)
    log.debug('substruct came up with pattern %s'%(patterns))
    
    # pattern is made on FieldType, 
    #so we need to dequeue self.fields at the same time to enqueue in myfields
    for nb, fieldTypes in patterns:
      if nb == 1:
        field = self.fields.pop(0)
        myfields.append(field) # single el
        #log.debug('simple field:%s '%(field) )
      elif len(fieldTypes) > 1: #  array of subtructure DEBUG XXX TODO
        log.debug('fieldTypes:%s'%fieldTypes)
        log.debug('substructure with sig %s'%(''.join([ft.sig[0] for ft in fieldTypes])  ))
        myelements=[]
        for i in range(nb):
          fields = [ self.fields.pop(0) for i in range(len(fieldTypes)) ] # nb-1 left
          #otherFields = [ self.fields.pop(0) for i in range((nb-1)*len(fieldTypesAndSizes)) ] 
          # need global ref to compare substructure signature to other anonstructure
          firstField = FieldType.makeStructField(self, fields[0].offset, fields)
          myelements.append(firstField)
        array = makeArrayField(self, myelements )
        myfields.append(array) 
        #log.debug('array of structure %s'%(array))
      elif len(fieldTypes) == 1: #make array of elements obase on same base type
        log.debug('found array of %s'%(self.fields[0].typename.basename))
        fields = [ self.fields.pop(0) for i in range(nb) ]
        array = makeArrayField(self, fields )
        myfields.append(array) 
        #log.debug('array of elements %s'%(array))
      else: # TODO DEBUG internal struct
        raise ValueError('fields patterns len is incorrect %d'%(len(fieldTypes)))
    
    log.debug('done with findSubstructure')    
    self.fields = myfields
    #print 'final', self.fields
    return

  
  def _aggZeroesBetweenIntArrays(self):
    if len(self.fields) < 3:
      return
    
    myfields = sorted(self.fields)
    i = 0
    while ( i < len(myfields) - 3 ):
      prev = myfields[i]
      field = myfields[i+1]
      next = myfields[i+2]
      if prev.isArray() and next.isArray() and field.isZeroes() and (
          fieldtypes.isIntegerType(prev.basicTypename) and
          fieldtypes.isIntegerType(next.basicTypename) ):
        # we have zeroes in the middle
        fieldLen = len(field)
        nbWord = fieldLen//Config.WORDSIZE 
        if (fieldLen % Config.WORDSIZE == 0) and  nbWord < 4: # more than 3 word zerroes it is probably another buffer
          # concat prev, field and next arrays to get on array
          newFields = prev.elements+[field]
          field.checkSmallInt() # force it in a small integer
          if nbWord > 1:
            for offsetadd in range(Config.WORDSIZE, Config.WORDSIZE*nbWord, Config.WORDSIZE):
              newFields.append(self._addField(field.offset+offsetadd, FieldType.SMALLINT, Config.WORDSIZE, False))
          newFields.extend(next.elements)
          # make an array for newFields and insert it in place of prev+field+next
          # pop prev, newfields and next, and put them in an array
          print 'aggZeroes', i, len(newFields)#, ','.join([f.toString('') for f in newFields])
          drop = [ myfields.pop(i) for x in range(3) ] #prev, 
          array = makeArrayField(self, newFields )          
          myfields.insert(i, array)
      #
      i+=1
    self.fields = myfields
    return

  '''
  Check if head or tail ( excluding zeroes) is different from the lot ( not common )
  '''
  def _excludeSizeVariableFromIntArray(self):
    if len(self.fields) < 2:
      return
    intArrays = [ f for f in self.fields if f.isArray() and fieldtypes.isIntegerType(f.basicTypename)]
    
    ''' nested func will explode the array fields in 3 fields '''
    def cutInThree():
      log.info('cutting in three %d %d %d'%(ind, nbSeen, val))
      # cut array in three
      index = self.fields.index(_arrayField)
      oldArray = self.fields.pop(index) # cut it from self.fields
      # cut the field in 3 parts ( ?zerroes, val, list)
      # add the rest
      if len(_arrayField.elements[ind+1:]) > 1: # add zerroes in front
        self.fields.insert(index, makeArrayField(self, _arrayField.elements[ind+1:]) )
      elif len(_arrayField.elements[ind+1:]) == 1: # add zero field
        self.fields.insert(index, _arrayField.elements[ind+1])
      # add the value
      self.fields.insert(index, _arrayField.elements[ind])
      # add zerroes in front
      if ind > 1: 
        self.fields.insert(index, makeArrayField(self, _arrayField.elements[:ind]) )
      elif ind == 1: # add zero field
        self.fields.insert(index, _arrayField.elements[0])
      #end
    
    print '%d intArrays'%(len(intArrays))     
    for _arrayField in intArrays:
      values = [ f.value for f in _arrayField.elements ]
      if len(values) < 8: # small array, no interest.
        continue
      ## head
      ret  = self._chopImprobableHead(values)
      if ret is not None: 
        ind, nbSeen, val = ret
        cutInThree()
      print ('going choping reverse')
      ## tail
      values = [ f.value for f in _arrayField.elements ]
      values.reverse()
      ret  = self._chopImprobableHead(values)
      if ret is not None:
        ind, nbSeen, val = ret
        ind = len(values) - ind -1
        # cut the field in 3 parts ( ?zerroes, val, list)
        cutInThree()
    return
      
  def _chopImprobableHead(self, values):
    ctr = collections.Counter( values)
    searchFor = itertools.dropwhile(lambda x: x==0, values) # val
    try:
      val = searchFor.next() # get first non zerroe value
    except StopIteration,e:
      return None # all zerroes ???
    nbSeen = ctr[val]
    print('choping around... Looking at val:%d, nb:%d'%(val, nbSeen))
    if nbSeen > 2: # mostly one. two MIGHT be ok. three is totally out of question.
      print 'too much', val, values[:10] 
      return None
    ind = values.index(val)
    if ind > min(2,len(values)):
      log.debug('we found a different value %d at index %d, but it is stuck deep in the array. Not leveraging it.'%(val, ind))
      return None
    # here we have a value in head, with little reoccurrence in the list.
    # we can chop the head and limit the array to [ind+1:]
    return (ind, nbSeen, val)
  
  def todo(self):
    ## apply librairies structures search against heap before running anything else.
    ## that should cut our reverse time way down
    ## prioritize search per reverse struct size. limit to big struct and their graph.
    ## then make a Named/Anonymous Struct out of them
    
    ## Anonymous Struct should be a model.Structure.
    ## it has to be dynamic generated, but that should be ok. we need a AnonymouStructMaker
    ## in:anonymousStruct1  
    ## out:anonymousStruct2 # with new fields types and so on...
    ## each algo has to be an ASMaker, so we can chain them.
    #
    ## The controller/Reverser should keep structs coherency. and appli maker to each of them
    ## the controller can have different heuristics to apply to struct :
    ##     * aggregates: char[][], buffers
    ##     * type definition: substructs, final reverse type step, c++ objects, 

    ## if a zeroes field (%WORDSIZE == 0) is stuck between 2 Integer arrays
    ## make a big array of the 3 fields

    ## on each integer array of significant ( > 8 ) size, Count the number of values.
    ## if rare value ( stats distibution ?) are present at the end or beginning, move array boundaries to exclude thoses value in integer fields
    
    ## on each integer array, look indices for \x00
    ## if there is a regular interval between \x00 in the sequence ( 5 char then 0 ) then make some sub arrays, nul terminated

    ## aggregate pattern base on pure basic type without length ( string, pointer, array, integer, zeroes )
    ## that gives us other probable substructure

    ## in a structure starting with an integer
    ## check for his value against the structure size

    ## magic len approach on untyped bytearrays or array of int. - TRY TO ALIGN ON 2**x
    ## if len(fields[i:i+n]) == 4096 // ou un exposant de 2 > 63 # m = math.modf(math.log( l, 2)) %% m[0] == 0.0 && m[1]>5.0
    ## alors on a un buffer de taille l
    ## fields[i:i+n] ne devrait contenir que du zeroes, untyped et int
    
    ## for each untyped field > 64 check if first integer is not a small int by the way
    ## on intel/amd check for endianness to find network struct.
    
    return
  
  
  def _isPointerToString(self, field):
    # pointer is Resolved
    if not field.isPointer():
      return False
    if hasattr(field,'_ptr_to_ext_lib'):
      return False      
    #if not hasattr(field,'_target_field'):
    #  return False
    return field._target_field.isString()
    
  
  def getPointerFields(self):
    return [f for f in self.fields if f.isPointer()]
    
  def getSignature(self, text=False):
    if text:
      return ''.join(['%s%d'%(f.getSignature()[0].sig,f.getSignature()[1]) for f in self.fields])
    return [f.getSignature() for f in self.fields]

  def getTypeSignature(self, text=False):
    if text:
      return ''.join([f.getSignature()[0].sig.upper() for f in self.fields])
    return [f.getSignature()[0] for f in self.fields]
  
  def toString(self):
    #FIXME : self._fixGaps() ## need to TODO overlaps
    #print self.fields
    fieldsString = '[ \n%s ]'% ( ''.join([ field.toString('\t') for field in self.fields]))
    info = 'resolved:%s SIG:%s'%(self.resolved, self.getSignature(text=True))
    if len(self.getPointerFields()) != 0:
      info += ' pointerResolved:%s'%(self.pointerResolved)
    ctypes_def = '''
class %s(LoadableMembers):  # %s
  _fields_ = %s

''' % (self, info, fieldsString)
    return ctypes_def

  def __contains__(self, other):
    if isinstance(other, numbers.Number):
      # test vaddr in struct instance len
      if self.vaddr <= other <= self.vaddr+len(self):
        return True
      return False
    else:
      raise NotImplementedError()
      
  def __getitem__(self, i):
    return self.fields[i]
    
  def __len__(self):
    return len(self.bytes)

  def __cmp__(self, other):
    if not isinstance(other, AnonymousStructInstance):
      raise TypeError
    return cmp(self.vaddr, other.vaddr)
  
  def __str__(self):
    return 'AnonymousStruct_%s_%s'%(len(self), self.prefixname )
  


