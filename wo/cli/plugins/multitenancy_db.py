"""WordOps Multi-tenancy Database Module
Handles database operations for multi-tenancy infrastructure.
"""

import os
import json
from datetime import datetime
from wo.core.logging import Log
from wo.core.database import db_session, Base
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text


class MultitenancyConfig(Base):
    """Multi-tenancy configuration table"""
    __tablename__ = 'multitenancy_config'
    
    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MultitenancyRelease(Base):
    """WordPress release tracking table"""
    __tablename__ = 'multitenancy_releases'
    
    id = Column(Integer, primary_key=True)
    release_name = Column(String(255), unique=True, nullable=False)
    wp_version = Column(String(50))
    is_current = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class MultitenancySite(Base):
    """Shared sites tracking table"""
    __tablename__ = 'multitenancy_sites'
    
    id = Column(Integer, primary_key=True)
    domain = Column(String(255), unique=True, nullable=False)
    site_type = Column(String(50))
    cache_type = Column(String(50))
    site_path = Column(String(255))
    php_version = Column(String(10))
    shared_release = Column(String(255))
    baseline_version = Column(Integer, default=0)
    is_enabled = Column(Boolean, default=True)
    is_ssl = Column(Boolean, default=False)
    is_staging = Column(Boolean, default=False)
    is_quarantined = Column(Boolean, default=False)
    quarantine_reason = Column(Text)
    quarantine_date = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MTDatabase:
    """Multi-tenancy database operations"""
    
    @staticmethod
    def initialize_tables(app):
        """Create multi-tenancy tables if they don't exist"""
        try:
            # Create tables in WordOps main database (dbase.db)
            Base.metadata.create_all(bind=db_session.bind)
            
            Log.debug(app, "Multi-tenancy database tables initialized")
            
            # *** PHASE 2 MIGRATION: Add new columns if they don't exist ***
            from sqlalchemy import text, inspect
            
            try:
                inspector = inspect(db_session.bind)
                existing_columns = [col['name'] for col in inspector.get_columns('multitenancy_sites')]
                
                migration_needed = False
                
                if 'is_staging' not in existing_columns:
                    migration_needed = True
                    Log.info(app, "Running Phase 2 database migration...")
                    
                    # Add columns one by one with proper error handling
                    try:
                        db_session.execute(text("""
                            ALTER TABLE multitenancy_sites 
                            ADD COLUMN is_staging BOOLEAN DEFAULT 0
                        """))
                        Log.debug(app, "Added is_staging column")
                    except Exception:
                        pass  # Column might already exist
                    
                    try:
                        db_session.execute(text("""
                            ALTER TABLE multitenancy_sites 
                            ADD COLUMN is_quarantined BOOLEAN DEFAULT 0
                        """))
                        Log.debug(app, "Added is_quarantined column")
                    except Exception:
                        pass
                    
                    try:
                        db_session.execute(text("""
                            ALTER TABLE multitenancy_sites 
                            ADD COLUMN quarantine_reason TEXT
                        """))
                        Log.debug(app, "Added quarantine_reason column")
                    except Exception:
                        pass
                    
                    try:
                        db_session.execute(text("""
                            ALTER TABLE multitenancy_sites 
                            ADD COLUMN quarantine_date DATETIME
                        """))
                        Log.debug(app, "Added quarantine_date column")
                    except Exception:
                        pass
                    
                    db_session.commit()
                    Log.info(app, "✅ Phase 2 database migration completed")
                    Log.info(app, "   Added: is_staging, is_quarantined, quarantine_reason, quarantine_date")
                
            except Exception as migration_error:
                Log.debug(app, f"Migration check/execution: {migration_error}")
                # Don't fail initialization if migration fails
            
        except Exception as e:
            Log.debug(app, f"Failed to initialize multi-tenancy tables: {e}")

    @staticmethod
    def is_initialized(app):
        """Check if multi-tenancy is initialized"""
        try:
            session = db_session
            config = session.query(MultitenancyConfig).filter_by(
                key='initialized'
            ).first()
            return config is not None and config.value == 'true'
        except:
            return False
    
    @staticmethod
    def save_config(app, config_dict):
        """Save configuration to database"""
        try:
            session = db_session
            for key, value in config_dict.items():
                config = session.query(MultitenancyConfig).filter_by(
                    key=key
                ).first()

                if config:
                    config.value = str(value)
                    config.updated_at = datetime.now()
                else:
                    config = MultitenancyConfig(
                        key=key,
                        value=str(value)
                    )
                    session.add(config)
            
            # Mark as initialized
            initialized = session.query(MultitenancyConfig).filter_by(
                key='initialized'
            ).first()
            
            if not initialized:
                initialized = MultitenancyConfig(
                    key='initialized',
                    value='true'
                )
                session.add(initialized)
            
            session.commit()
            Log.debug(app, "Configuration saved to database")
                
        except Exception as e:
            Log.error(app, f"Failed to save configuration: {e}")
    
    @staticmethod
    def get_config(app, key):
        """Get configuration value from database"""
        try:
            session = db_session
            config = session.query(MultitenancyConfig).filter_by(
                key=key
            ).first()
            
            if config:
                return config.value
            return None
                
        except Exception as e:
            Log.debug(app, f"Failed to get config {key}: {e}")
            return None
    
    @staticmethod
    def get_current_release(app):
        """Get current active release"""
        try:
            session = db_session
            release = session.query(MultitenancyRelease).filter_by(
                is_current=True
            ).first()
            
            if release:
                return release.release_name
            
            # Fallback to config
            return MTDatabase.get_config(app, 'current_release')
                
        except Exception as e:
            Log.debug(app, f"Failed to get current release: {e}")
            return None
    
    @staticmethod
    def update_release(app, release_name):
        """Update current release"""
        try:
            session = db_session
            # Mark all releases as not current
            session.query(MultitenancyRelease).update(
                {'is_current': False}
            )
            
            # Check if release exists
            release = session.query(MultitenancyRelease).filter_by(
                release_name=release_name
            ).first()
            
            if release:
                release.is_current = True
            else:
                # Create new release entry
                release = MultitenancyRelease(
                    release_name=release_name,
                    is_current=True
                )
                session.add(release)
            
            # Update config
            config = session.query(MultitenancyConfig).filter_by(
                key='current_release'
            ).first()
            
            if config:
                config.value = release_name
                config.updated_at = datetime.now()
            else:
                config = MultitenancyConfig(
                    key='current_release',
                    value=release_name
                )
                session.add(config)
            
            session.commit()
            Log.debug(app, f"Updated current release to {release_name}")
                
        except Exception as e:
            Log.error(app, f"Failed to update release: {e}")
    
    @staticmethod
    def get_baseline_version(app):
        """Get current baseline version"""
        try:
            version = MTDatabase.get_config(app, 'baseline_version')
            return int(version) if version else 1
        except:
            return 1
    
    @staticmethod
    def increment_baseline_version(app):
        """Increment baseline version to trigger reapplication"""
        try:
            current = MTDatabase.get_baseline_version(app)
            new_version = current + 1
            
            session = db_session
            config = session.query(MultitenancyConfig).filter_by(
                key='baseline_version'
            ).first()
            
            if config:
                config.value = str(new_version)
                config.updated_at = datetime.now()
            else:
                config = MultitenancyConfig(
                    key='baseline_version',
                    value=str(new_version)
                )
                session.add(config)
            
            session.commit()
            Log.debug(app, f"Incremented baseline version to {new_version}")
                
        except Exception as e:
            Log.error(app, f"Failed to increment baseline version: {e}")
    
    @staticmethod
    def add_shared_site(app, domain, site_data):
        """Add a site to shared sites tracking"""
        try:
            session = db_session
            # Check if site already exists
            site = session.query(MultitenancySite).filter_by(
                domain=domain
            ).first()
            
            if site:
                # Update existing site
                for key, value in site_data.items():
                    if hasattr(site, key):
                        setattr(site, key, value)
                site.updated_at = datetime.now()
            else:
                # Create new site entry
                site = MultitenancySite(
                    domain=domain,
                    site_type=site_data.get('site_type', 'wp'),
                    cache_type=site_data.get('cache_type', 'basic'),
                    site_path=site_data.get('site_path', f'/var/www/{domain}'),
                    php_version=site_data.get('php_version', '8.3'),
                    shared_release=site_data.get('shared_release'),
                    is_ssl=site_data.get('is_ssl', False)
                )
                session.add(site)
            
            session.commit()
            Log.debug(app, f"Added shared site: {domain}")
                
        except Exception as e:
            Log.error(app, f"Failed to add shared site: {e}")
    
    @staticmethod
    def get_shared_sites(app):
        """Get list of all shared sites"""
        try:
            session = db_session
            sites = session.query(MultitenancySite).all()
            
            result = []
            for site in sites:
                result.append({
                    'domain': site.domain,
                    'site_type': site.site_type,
                    'cache_type': site.cache_type,
                    'site_path': site.site_path,
                    'php_version': site.php_version,
                    'shared_release': site.shared_release,
                    'baseline_version': site.baseline_version,
                    'is_enabled': site.is_enabled,
                    'is_ssl': site.is_ssl,
                    'created_at': site.created_at,
                    'updated_at': site.updated_at
                })
            
            return result
                
        except Exception as e:
            Log.debug(app, f"Failed to get shared sites: {e}")
            return []
    
    @staticmethod
    def is_shared_site(app, domain):
        """Check if a site is using shared core"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(
                domain=domain
            ).first()
            return site is not None
                
        except:
            return False
    
    @staticmethod
    def remove_shared_site(app, domain):
        """Remove a site from shared sites tracking"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(
                domain=domain
            ).first()
            
            if site:
                session.delete(site)
                session.commit()
                Log.debug(app, f"Removed shared site: {domain}")
                return True
            
            return False
                
        except Exception as e:
            Log.error(app, f"Failed to remove shared site: {e}")
            return False
    
    @staticmethod
    def update_site_baseline(app, domain, version):
        """Update baseline version for a site"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(
                domain=domain
            ).first()
            
            if site:
                site.baseline_version = version
                site.updated_at = datetime.now()
                session.commit()
                Log.debug(app, f"Updated baseline version for {domain} to {version}")
                return True
            
            return False
                
        except Exception as e:
            Log.error(app, f"Failed to update site baseline: {e}")
            return False
    
    @staticmethod
    def cleanup(app):
        """Clean up multi-tenancy database entries"""
        try:
            session = db_session
            # Delete all multi-tenancy config
            session.query(MultitenancyConfig).delete()
            
            # Delete all releases
            session.query(MultitenancyRelease).delete()
            
            # Delete all shared sites
            session.query(MultitenancySite).delete()
            
            session.commit()
            Log.debug(app, "Cleaned up multi-tenancy database")
                
        except Exception as e:
            Log.error(app, f"Failed to cleanup database: {e}")
    
    @staticmethod
    def get_stats(app):
        """Get multi-tenancy statistics"""
        try:
            session = db_session
            total_sites = session.query(MultitenancySite).count()
            enabled_sites = session.query(MultitenancySite).filter_by(
                is_enabled=True
            ).count()
            ssl_sites = session.query(MultitenancySite).filter_by(
                is_ssl=True
            ).count()
            total_releases = session.query(MultitenancyRelease).count()
            
            # Get PHP version distribution
            php_stats = {}
            sites = session.query(MultitenancySite).all()
            for site in sites:
                php_ver = site.php_version or 'unknown'
                php_stats[php_ver] = php_stats.get(php_ver, 0) + 1
            
            # Get cache type distribution
            cache_stats = {}
            for site in sites:
                cache = site.cache_type or 'none'
                cache_stats[cache] = cache_stats.get(cache, 0) + 1
            
            return {
                'total_sites': total_sites,
                'enabled_sites': enabled_sites,
                'ssl_sites': ssl_sites,
                'total_releases': total_releases,
                'php_distribution': php_stats,
                'cache_distribution': cache_stats
            }
                
        except Exception as e:
            Log.debug(app, f"Failed to get stats: {e}")
            return {}

    @staticmethod
    def migrate_schema(app):
        """Migrate database schema to add new columns if they don't exist"""
        try:
            from sqlalchemy import inspect
            session = db_session
            inspector = inspect(session.bind)
            
            # Get current columns
            columns = [col['name'] for col in inspector.get_columns('multitenancy_sites')]
            
            # Check which columns need to be added
            new_columns = []
            if 'is_staging' not in columns:
                new_columns.append("ALTER TABLE multitenancy_sites ADD COLUMN is_staging BOOLEAN DEFAULT 0")
            if 'is_quarantined' not in columns:
                new_columns.append("ALTER TABLE multitenancy_sites ADD COLUMN is_quarantined BOOLEAN DEFAULT 0")
            if 'quarantine_reason' not in columns:
                new_columns.append("ALTER TABLE multitenancy_sites ADD COLUMN quarantine_reason TEXT")
            if 'quarantine_date' not in columns:
                new_columns.append("ALTER TABLE multitenancy_sites ADD COLUMN quarantine_date DATETIME")
            
            # Execute migrations
            if new_columns:
                for sql in new_columns:
                    session.execute(sql)
                session.commit()
                Log.debug(app, f"Migrated database: added {len(new_columns)} new columns")
                return True
            else:
                Log.debug(app, "Database schema is up to date")
                return False
                
        except Exception as e:
            Log.debug(app, f"Schema migration error: {e}")
            return False
    
    @staticmethod
    def get_staging_site(app):
        """Get the staging site"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(
                is_staging=True,
                is_enabled=True
            ).first()
            
            if site:
                return {
                    'id': site.id,
                    'domain': site.domain,
                    'site_type': site.site_type,
                    'cache_type': site.cache_type,
                    'site_path': site.site_path,
                    'php_version': site.php_version,
                    'shared_release': site.shared_release,
                    'baseline_version': site.baseline_version,
                    'is_enabled': site.is_enabled,
                    'is_ssl': site.is_ssl,
                    'is_staging': site.is_staging,
                    'is_quarantined': site.is_quarantined
                }
            return None
            
        except Exception as e:
            Log.debug(app, f"Error getting staging site: {e}")
            return None
    
    @staticmethod
    def mark_site_quarantined(app, domain, reason):
        """Mark a site as quarantined"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(domain=domain).first()
            
            if site:
                site.is_quarantined = True
                site.quarantine_reason = reason
                site.quarantine_date = datetime.now()
                site.updated_at = datetime.now()
                session.commit()
                Log.debug(app, f"Quarantined site: {domain}")
                return True
            
            return False
            
        except Exception as e:
            Log.debug(app, f"Error quarantining site: {e}")
            return False
    
    @staticmethod
    def unquarantine_site(app, domain):
        """Remove quarantine status from a site"""
        try:
            session = db_session
            site = session.query(MultitenancySite).filter_by(domain=domain).first()
            
            if site:
                site.is_quarantined = False
                site.quarantine_reason = None
                site.quarantine_date = None
                site.updated_at = datetime.now()
                session.commit()
                Log.debug(app, f"Unquarantined site: {domain}")
                return True
            
            return False
            
        except Exception as e:
            Log.debug(app, f"Error unquarantining site: {e}")
            return False
    
    @staticmethod
    def get_quarantined_sites(app):
        """Get all quarantined sites"""
        try:
            session = db_session
            sites = session.query(MultitenancySite).filter_by(
                is_quarantined=True
            ).order_by(MultitenancySite.quarantine_date.desc()).all()
            
            return [{
                'domain': site.domain,
                'quarantine_reason': site.quarantine_reason,
                'quarantine_date': site.quarantine_date.isoformat() if site.quarantine_date else None
            } for site in sites]
            
        except Exception as e:
            Log.debug(app, f"Error getting quarantined sites: {e}")
            return []
