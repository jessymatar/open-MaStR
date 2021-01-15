import datetime
import os
from sqlalchemy.orm import sessionmaker, Query
from sqlalchemy import and_, create_engine, func
from sqlalchemy.sql import exists
import shlex
import subprocess

from open_mastr.soap_api.config import setup_logger
from open_mastr.soap_api.download import MaStRDownload, _flatten_dict
import open_mastr.soap_api.db_models as db


log = setup_logger()

engine = create_engine(
    "postgresql+psycopg2://open-mastr:open-mastr@localhost:55443/open-mastr", echo=False
)
Session = sessionmaker(bind=engine)
session = Session()


def chunks(lst, n):
    """Yield successive n-sized chunks from lst.

    `Credits <https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks>`_
    """
    if isinstance(lst, Query):
        length = lst.count()
    else:
        length = len(lst)
    for i in range(0, length, n):
        yield lst[i: i + n]


class MaStRReflected:
    def __init__(self, empty_schema=False, restore_dump=None, initialize_db=True, parallel_processes=None):

        # Spin up database container
        if initialize_db:
            self.initdb()

        # Create database tables
        with engine.connect().execution_options(autocommit=True) as con:
            if empty_schema:
                con.execute(f"DROP SCHEMA IF EXISTS {db.Base.metadata.schema} CASCADE;")
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {db.Base.metadata.schema};")
        db.Base.metadata.create_all(engine)

        # Associate downloader
        self.mastr_dl = MaStRDownload(parallel_processes=parallel_processes)

        # Restore datadb.Base from a dump
        if restore_dump:
            self.restore(restore_dump)

        # Map technologies on ORMs
        self.orm_map = {
            "wind": {
                "unit_data": "WindExtended",
                "eeg_data": "WindEeg",
                "permit_data": "Permit"
            },
            "solar": {
                "unit_data": "SolarExtended",
                "eeg_data": "SolarEeg",
                "permit_data": "Permit"
            },
            "biomass": {
                "unit_data": "BiomassExtended",
                "eeg_data": "BiomassEeg",
                "kwk_data": "Kwk",
                "permit_data": "Permit"
            },
            "combustion": {
                "unit_data": "CombustionExtended",
                "kwk_data": "Kwk",
                "permit_data": "Permit"
            },
            "gsgk": {
                "unit_data": "GsgkExtended",
                "kwk_data": "Kwk",
                "permit_data": "Permit"
            },
            "hydro": {
                "unit_data": "HydroExtended",
                "permit_data": "Permit"
            },
            "nuclear": {
                "unit_data": "NuclearExtended",
                "permit_data": "Permit"
            },
            "storage": {
                "unit_data": "StorageExtended",
                "permit_data": "Permit"
            },
        }

        # Map technology and MaStR unit type
        # Map technologies on ORMs
        self.unit_type_map = {
            "Windeinheit": "wind",
            "Solareinheit": "solar",
            "Biomasse": "biomass",
            "Wasser": "hydro",
            "Geothermie": "gsgk",
            "Verbrennung": "combustion",
            "Kernenergie": "nuclear",
            "Stromspeichereinheit": "storage",
            "Gasspeichereinheit": "gas_storage",
            "Gasverbrauchseinheit": "gas_consumer",
            "Stromverbrauchseinheit": "consumer",
            "Gaserzeugungseinheit": "gas_producer",
        }

    def initdb(self):
        """ Initialize the local datadb.Base used for data processing."""
        conf_file_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )

        subprocess.run(
            ["docker-compose", "up", "-d"],
            cwd=conf_file_path,
        )

    def backfill_basic(self, technology=None, date=None, limit=None):
        """Backfill basic unit data.

        Fill database table 'basic_units' with data. It allows specification of which data should be retrieved via
        the described parameter options.

        Parameters
        ----------
        technology: str or list
            Specify technologies for which data should be backfilled.

            * 'solar' (`str`): Backfill data for a single technology.
            * ['solar', 'wind'] (`list`):  Backfill data for multiple technologies given in a list.
            * `None`: Backfill data for all technologies

            Defaults to `None`.
        date: None, :class:`datetime.datetime`, str
            Specify backfill date from which on data is retrieved

            Only data with modification time stamp greater that `date` is retrieved.

            * `datetime.datetime(2020, 11, 27)`: Retrieve data which is is newer than this time stamp
            * 'latest': Retrieve data which is newer than the newest data already in the table.
              It is aware of a different 'latest date' for each technology. Hence, it works in combination with
              `technology=None` and `technology=["wind", "solar"]` for example.

              .. warning::

                 Don't use 'latest' in combination with `limit`. This might lead to unexpected results.
            * `None`: Complete backfill

            Defaults to `None`.
        limit: int
            Maximum number of units.
            Defaults to `None` which means no limit is set and all available data is queried. Use with care!
        """
        reversed_unit_type_map = {v: k for k, v in self.unit_type_map.items()}

        # Create list of technologies to backfill
        if isinstance(technology, str):
            technology_list = [technology]
        elif technology == None:
            technology_list = [None]
        elif isinstance(technology, list):
            technology_list = technology

        # Set limit to a number >> number of units of technology with most units
        if limit is None:
            limit = 10 ** 8

        if date == "latest":
            dates = []
            for tech in technology_list:
                if tech:
                    # In case technologies are specified, latest data date gets queried per technology
                    newest_date = session.query(db.BasicUnit.DatumLetzeAktualisierung).filter(
                        db.BasicUnit.Einheittyp == reversed_unit_type_map[tech]).order_by(
                        db.BasicUnit.DatumLetzeAktualisierung.desc()).first()
                else:
                    # If technologies aren't defined ([None]) latest date per technology is queried in query
                    # This also leads that the remainder of the loop body is skipped
                    subquery = session.query(db.BasicUnit.Einheittyp,
                                             func.max(db.BasicUnit.DatumLetzeAktualisierung).label("maxdate")).group_by(
                        db.BasicUnit.Einheittyp)
                    dates = [s[1] for s in subquery]
                    technology_list = [self.unit_type_map[s[0]] for s in subquery]
                    # Break the for loop over technology here, because we write technology_list and dates at once
                    break

                # Add date to dates list
                if newest_date:
                    dates.append(newest_date[0])
                # Cover the case where no data is in the database and latest is still used
                else:
                    dates.append(None)
        else:
            dates = [date] * len(technology_list)

        # Retrieve data for each technology separately
        for tech, date in zip(technology_list, dates):
            log.info(f"Backfill data for technology {tech}")

            # Catch weird MaStR SOAP response
            basic_units = self.mastr_dl.basic_unit_data(tech, limit, date_from=date)

            # Insert basic data into databse
            log.info("Insert basic unit data into DB and submit additional data requests")
            for basic_units_chunk in basic_units:
                # Make sure that no duplicates get inserted into database (would result in an error)
                # Only new data gets inserted or data with newer modification date gets updated

                # Remove duplicates returned from API
                basic_units_chunk_unique = [
                    unit
                    for n, unit in enumerate(basic_units_chunk)
                    if unit["EinheitMastrNummer"]
                       not in [_["EinheitMastrNummer"] for _ in basic_units_chunk[n + 1:]]
                ]
                basic_units_chunk_unique_ids = [_["EinheitMastrNummer"] for _ in basic_units_chunk_unique]

                # Find units that are already in the DB
                common_ids = [_.EinheitMastrNummer for _ in session.query(db.BasicUnit.EinheitMastrNummer).filter(
                    db.BasicUnit.EinheitMastrNummer.in_(basic_units_chunk_unique_ids))]

                # Create instances for new data and for updated data
                insert = []
                updated = []
                for unit in basic_units_chunk_unique:
                    # In case data for the unit already exists, only update if new data is newer
                    if unit["EinheitMastrNummer"] in common_ids:
                        if session.query(exists().where(
                                and_(db.BasicUnit.EinheitMastrNummer == unit["EinheitMastrNummer"],
                                     db.BasicUnit.DatumLetzeAktualisierung < unit[
                                         "DatumLetzeAktualisierung"]))).scalar():
                            updated.append(unit)
                            session.merge(db.BasicUnit(**unit))
                    # In case of new data, just insert
                    else:
                        insert.append(unit)
                session.bulk_save_objects([db.BasicUnit(**u) for u in insert])
                inserted_and_updated = insert + updated

                # Submit additional data requests
                extended_data = []
                eeg_data = []
                kwk_data = []
                permit_data = []

                for basic_unit in inserted_and_updated:
                    # Extended unit data
                    extended_data.append(
                        {
                            "EinheitMastrNummer": basic_unit["EinheitMastrNummer"],
                            "additional_data_id": basic_unit["EinheitMastrNummer"],
                            "technology": self.unit_type_map[basic_unit["Einheittyp"]],
                            "data_type": "unit_data",
                            "request_date": datetime.datetime.now(tz=datetime.timezone.utc),
                        }
                    )

                    # EEG unit data
                    if basic_unit["EegMastrNummer"]:
                        eeg_data.append(
                            {
                                "EinheitMastrNummer": basic_unit["EinheitMastrNummer"],
                                "additional_data_id": basic_unit["EegMastrNummer"],
                                "technology": self.unit_type_map[basic_unit["Einheittyp"]],
                                "data_type": "eeg_data",
                                "request_date": datetime.datetime.now(tz=datetime.timezone.utc),
                            }
                        )

                    # KWK unit data
                    if basic_unit["KwkMastrNummer"]:
                        kwk_data.append(
                            {
                                "EinheitMastrNummer": basic_unit["EinheitMastrNummer"],
                                "additional_data_id": basic_unit["KwkMastrNummer"],
                                "technology": self.unit_type_map[basic_unit["Einheittyp"]],
                                "data_type": "kwk_data",
                                "request_date": datetime.datetime.now(tz=datetime.timezone.utc),
                            }
                        )

                    # Permit unit data
                    if basic_unit["GenMastrNummer"]:
                        permit_data.append(
                            {
                                "EinheitMastrNummer": basic_unit["EinheitMastrNummer"],
                                "additional_data_id": basic_unit["GenMastrNummer"],
                                "technology": self.unit_type_map[basic_unit["Einheittyp"]],
                                "data_type": "permit_data",
                                "request_date": datetime.datetime.now(tz=datetime.timezone.utc),
                            }
                        )

                    # Delete old entries for additional data requests
                    session.query(db.AdditionalDataRequested).filter(
                        db.AdditionalDataRequested.EinheitMastrNummer == basic_unit["EinheitMastrNummer"],
                        db.AdditionalDataRequested.technology == self.unit_type_map[basic_unit["Einheittyp"]],
                        db.AdditionalDataRequested.request_date < datetime.datetime.now(tz=datetime.timezone.utc)
                    ).delete()

                # Flush delete statements to database
                session.commit()

                # Insert new requests for additional data
                session.bulk_insert_mappings(db.AdditionalDataRequested, extended_data)
                session.bulk_insert_mappings(db.AdditionalDataRequested, eeg_data)
                session.bulk_insert_mappings(db.AdditionalDataRequested, kwk_data)
                session.bulk_insert_mappings(db.AdditionalDataRequested, permit_data)

                session.commit()
            session.close()
            log.info("Backfill successfully finished")

    def retrieve_additional_data(self, technology, data_type, limit=None, chunksize=1000):

        # Mapping of download from MaStRDownload
        download_functions = {
            "unit_data": "_extended_unit_data",
            "eeg_data": "_eeg_unit_data",
            "kwk_data": "_kwk_unit_data",
            "permit_data": "_permit_unit_data",
        }

        if limit:
            if chunksize > limit:
                chunksize = limit

        units_queried = 0
        while units_queried < limit:

            requested_chunk = session.query(db.AdditionalDataRequested).filter(
                and_(db.AdditionalDataRequested.data_type == data_type,
                     db.AdditionalDataRequested.technology == technology)).limit(chunksize)

            ids = [_.additional_data_id for _ in requested_chunk]

            number_units_merged = 0
            if ids:
                # Retrieve data
                unit_data, missed_units = self.mastr_dl._additional_data(technology, ids, download_functions[data_type])
                unit_data = _flatten_dict(unit_data)
                log.info(f"Additional data retrieved for {len(unit_data)}")

                # Prepare data and add to database table

                for unit_dat in unit_data:
                    # Remove query status information from response
                    for exclude in ["Ergebniscode", "AufrufVeraltet", "AufrufVersion", "AufrufLebenszeitEnde"]:
                        del unit_dat[exclude]

                    # Create new instance and update potentially existing one
                    unit = getattr(db, self.orm_map[technology][data_type])(**unit_dat)
                    session.merge(unit)
                    number_units_merged += 1

                session.commit()
                log.info(f"{number_units_merged} units merged and committed")
                # Log units where data retrieval was not successful
                for missed_unit in missed_units:
                    missed = db.MissedAdditionalData(additional_data_id=missed_unit)
                    session.add(missed)

                log.info(f"Missed units ({len(missed_units)} logged")
                # Remove units from additional data request table if additional data was retrieved
                for requested_unit in requested_chunk:
                    if requested_unit.additional_data_id not in missed_units:
                        session.delete(requested_unit)

                log.info("Units deleted")
                # Send to datadb.Base complete transactions
                session.commit()

                # Update while iteration condition
                units_queried += len(ids)

            if number_units_merged == 0:
                log.info("No further data is requested")
                break

    def dump(self, dumpfile="open-mastr-continuous-update.backup"):
        """
        Dump MaStR datadb.Base.

        Parameters
        ----------
        dumpfile : str or path-like, optional
            Save path for dump including filename. When only a filename is given, the dump is saved to CWD.
        """
        dump_cmd = f"pg_dump -Fc " \
            f"-f {dumpfile} " \
            f"-n mastr_mirrored " \
            f"-h localhost " \
            f"-U open-mastr " \
            f"-p 55443 " \
            f"open-mastr"

        proc = subprocess.Popen(dump_cmd, shell=True, env={
            'PGPASSWORD': "open-mastr"
        })
        proc.wait()

    def restore(self, dumpfile):
        """
        Restore the MaStR datadb.Base from an SQL dump.

        Parameters
        ----------
        dumpfile : str or path-like, optional
            Save path for dump including filename. When only a filename is given, the dump is restored from CWD.
        """
        # Interpret file name and path
        dump_file_dir, dump_file = os.path.split(dumpfile)
        cwd = os.path.abspath(os.path.dirname(dump_file_dir))


        # Define import of SQL dump with pg_restore
        restore_cmd = f"pg_restore -h localhost -U open-mastr -p 55443 -d open-mastr {dump_file}"
        restore_cmd = shlex.split(restore_cmd)

        # Execute restore command
        proc = subprocess.Popen(restore_cmd,
                                shell=False,
                                env={'PGPASSWORD': "open-mastr"},
                                cwd=cwd,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                )
        proc.wait()
