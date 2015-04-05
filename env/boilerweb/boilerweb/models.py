from sqlalchemy import (
    Column,
    Index,
    Integer,
    Text,
    DateTime,
    )

from sqlalchemy.ext.declarative import declarative_base

from sqlalchemy.orm import (
    scoped_session,
    sessionmaker,
    )

from zope.sqlalchemy import ZopeTransactionExtension

DBSession = scoped_session(sessionmaker(extension=ZopeTransactionExtension()))
Base = declarative_base()


class temperature(Base):
    __tablename__ = 'temperature'
    rowid = Column(Integer, primary_key=True)
    date = Column(DateTime, nullable=False)
    sensor = Column(Integer, nullable=False)
    temperature = Column(Integer, nullable=False)

Index('temperature_date', temperature.date, unique=True, mysql_length=255)

class channel(Base):
    __tablename__ = 'channel'
    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
