from datetime import date
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from connection_preferences import VOLTAGES_KV,DEFAULT_VOLTAGES_KV


class ProjectInput(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    name:str=Field(default="Mon projet solaire",min_length=1,max_length=100)
    country:Literal['France']='France'
    lat:float=Field(default=43.61,ge=41,le=51.5)
    lon:float=Field(default=3.88,ge=-5.5,le=10)
    radius:float=Field(default=10,ge=.1,le=1000)
    siting_mode:Literal['agrivoltaic','ground']='agrivoltaic'
    max_sectors:int=Field(default=6,ge=1,le=8)
    avoid_natural_zones:bool=True
    avoid_servitudes:bool=True
    legal_areas_geojson:dict|None=None
    legal_coverage_percent:float|None=Field(default=None,ge=0,le=100)
    proven_technology:bool=False
    agricultural_service:Literal['to_document','agronomic','climate','hazards','animal_welfare']='to_document'
    surface:float=Field(default=10000,ge=10,le=10_000_000)
    start:int=Field(default=date.today().year-1,ge=2022)
    end:int=Field(default=date.today().year-1,ge=2022)
    point_budget:int=Field(default=32,ge=4,le=32)
    weather_reuse_m:Literal[0,250,500,1000]=500
    connection_voltages_kv:list[float]=Field(default_factory=lambda:list(DEFAULT_VOLTAGES_KV),min_length=1,max_length=len(VOLTAGES_KV))
    tilt:float=Field(default=30,ge=0,le=60)
    pr:float=Field(default=.8,ge=.5,le=.95)
    density:float=Field(default=210,ge=100,le=300)
    coverage:float=Field(default=.4,ge=.1,le=.8)
    loss:float=Field(default=3,ge=0,le=20)
    move:float=Field(default=3,ge=.25,le=10)
    parcel_radius:float=Field(default=1,ge=.1,le=3)
    minimum_agricultural_share:float=Field(default=.8,ge=0,le=1)
    price_mode:Literal["Ventes locales comparables","Non chiffré","Hypothèse manuelle","Référence nationale"]="Ventes locales comparables"
    manual_price:float=Field(default=.65,ge=0,le=1000)
    price_uncertainty:float=Field(default=.5,ge=0,le=1)
    land_basis:Literal["Parcelles entières","Emprise PV seulement"]="Parcelles entières"
    automatic_ratings:bool=False
    inventory_csv:str|None=Field(default=None,max_length=2_000_000)
    cadastral_geojson:dict|None=None
    rpg_geojson:dict|None=None
    rpg_year:int=Field(default=2024,ge=2015,le=date.today().year)
    pv_low:float=Field(default=650,ge=0,le=10000)
    pv_high:float=Field(default=1200,ge=0,le=10000)
    cable_low:float=Field(default=80,ge=0,le=5000)
    cable_high:float=Field(default=250,ge=0,le=5000)
    fixed_low:float=Field(default=20000,ge=0,le=10_000_000)
    fixed_high:float=Field(default=100000,ge=0,le=10_000_000)
    transformer_low:float=Field(default=30000,ge=0,le=10_000_000)
    transformer_high:float=Field(default=80000,ge=0,le=10_000_000)
    build_transformer:bool=False
    detour:float=Field(default=1.3,ge=1,le=3)
    contingency:float=Field(default=.25,ge=0,le=1)
    collector_low:float=Field(default=40,ge=0,le=5000)
    collector_high:float=Field(default=120,ge=0,le=5000)
    collector_voltage:float=Field(default=20,ge=1,le=50)
    dc_ac_ratio:float=Field(default=1.2,ge=1,le=2)
    power_factor:float=Field(default=.95,ge=.5,le=1)
    transformer_margin:float=Field(default=.2,ge=0,le=1)

    @model_validator(mode="after")
    def validate_project(self):
        if any(v not in VOLTAGES_KV for v in self.connection_voltages_kv):raise ValueError('Choisir les tensions de raccordement parmi les valeurs proposées.')
        self.connection_voltages_kv=sorted(set(self.connection_voltages_kv))
        if self.legal_areas_geojson is not None:
            from france_rules import legal_perimeter
            from shapely.geometry import Point
            legal_perimeter(self.legal_areas_geojson,Point().buffer(0))
        if self.cadastral_geojson is not None or self.rpg_geojson is not None:
            from regulatory import validated_features
        if self.cadastral_geojson is not None:
            validated_features(self.cadastral_geojson,'Cadastre')
        if self.rpg_geojson is not None:
            self.rpg_geojson={**self.rpg_geojson,'features':validated_features(self.rpg_geojson,'RPG')}
        if self.start>self.end or self.end>=date.today().year:
            raise ValueError("Choisir des années complètes entre 2022 et l'année dernière.")
        for key in ("pv","cable","fixed","transformer","collector"):
            if getattr(self,key+"_low")>getattr(self,key+"_high"):
                raise ValueError(f"Fourchette de coût {key} inversée.")
        return self

    def assumptions(self):
        data={k:getattr(self,k) for k in ("pv_low","pv_high","cable_low","cable_high","fixed_low","fixed_high","detour","contingency","collector_low","collector_high","dc_ac_ratio","power_factor","transformer_margin","build_transformer")}
        data.update(transformer_low=self.transformer_low if self.build_transformer else 0,
                    transformer_high=self.transformer_high if self.build_transformer else 0,
                    transformer_quote_low=self.transformer_low,transformer_quote_high=self.transformer_high,
                    collector_voltage_kv=self.collector_voltage)
        return data


class NetworkInput(BaseModel):
    model_config=ConfigDict(extra="forbid",allow_inf_nan=False)
    country:Literal['France']='France'
    lat:float=Field(ge=41,le=51.5)
    lon:float=Field(ge=-5.5,le=10)
    radius:float=Field(default=3,ge=.1,le=10)
    connection_voltages_kv:list[float]=Field(default_factory=lambda:list(DEFAULT_VOLTAGES_KV),min_length=1,max_length=len(VOLTAGES_KV))

    @model_validator(mode="after")
    def validate_voltages(self):
        if any(v not in VOLTAGES_KV for v in self.connection_voltages_kv):raise ValueError('Choisir les tensions proposées.')
        self.connection_voltages_kv=sorted(set(self.connection_voltages_kv))
        return self
