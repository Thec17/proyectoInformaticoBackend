from sqlalchemy import Column, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Mawb(Base):
    __tablename__ = 'MAWBs'

    id = Column(Integer, primary_key=True, name='id_MAWBs')
    mawb_number = Column(String(50), unique=True, nullable=False, name='MAWB_number')
    total_expected_pcs = Column(Integer, nullable=True, default=0, name='MAWB_totalExpectedPcs')
    total_expected_wgt = Column(Float(asdecimal=True), nullable=True, default=0.0, name='MAWB_totalExpectedWgt')
    total_real_pcs = Column(Integer, nullable=True, default=0, name='MAWB_totalRealPcs')
    total_real_wgt = Column(Float(asdecimal=True), nullable=True, default=0.0, name='MAWB_totalRealWgt')
    date = Column(Date, nullable=False, name='MAWB_date')

    hawbs = relationship("Hawb", back_populates="mawb", cascade="all, delete-orphan")

class Hawb(Base):
    __tablename__ = 'HAWBs'

    id = Column(Integer, primary_key=True, name='id_HAWBs')
    hawb_number = Column(String(50), nullable=False, name='HAWB_number')
    expected_pcs = Column(Integer, nullable=True, default=0, name='HAWB_expectedPcs')
    expected_wgt = Column(Float(asdecimal=True), nullable=True, default=0.0, name='HAWB_expectedWgt')
    real_pcs = Column(Integer, nullable=True, default=0, name='HAWB_realPcs')
    real_wgt = Column(Float(asdecimal=True), nullable=True, default=0.0, name='HAWB_realWgt')
    mawb_id = Column(Integer, ForeignKey('MAWBs.id_MAWBs'), nullable=False, name='MAWBs_id_MAWBs')

    mawb = relationship("Mawb", back_populates="hawbs")
