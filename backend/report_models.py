"""A constrained report language: references select facts; prose cannot create figures."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION='trabo-report-1'
SECTION_TITLES={
 'executive':'Executive overview',
 'comparison':'Budget versus actual',
 'movements':'Material movements',
 'unknowns':'Unexplained amounts',
 'cash':'Cash and assumptions',
 'actions':'Proposed actions',
 'evidence':'Evidence appendix',
}
SectionKey=Literal['executive','comparison','movements','unknowns','cash','actions','evidence']
Template=Literal['summary','movement','driver','unknown','coverage','cash','assumptions','owner_note','scope']
EvidenceState=Literal['verified_arithmetic','supported','owner_confirmed','hypothesis','unknown','modeled']

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Claim(StrictModel):
    template:Template
    reference_id:str
    period:str
    amount_unit:Literal['INR paise']
    direction:Literal['adverse','favorable','neutral','not_applicable']
    evidence_state:EvidenceState
    wording:Literal['standard','concise']
    amount_minor:int|None
    percentage:str|None

class Section(StrictModel):
    key:SectionKey
    commentary:str=Field(max_length=3000)
    claims:list[Claim]=Field(max_length=1000)

class Action(StrictModel):
    reference_id:str
    instruction:str=Field(min_length=3,max_length=1000)
    owner:str=Field(max_length=100)

class OpenQuestion(StrictModel):
    reference_id:str
    text:str=Field(min_length=3,max_length=1000)

class ReportDocument(StrictModel):
    schema_version:Literal['trabo-report-1']
    snapshot_id:str
    period:str
    amount_unit:Literal['INR paise']
    sections:list[Section]=Field(min_length=7,max_length=7)
    actions:list[Action]=Field(min_length=1,max_length=50)
    questions:list[OpenQuestion]=Field(max_length=50)
