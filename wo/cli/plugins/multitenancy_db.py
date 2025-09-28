"""WordOps Multi-tenancy Database Module
Handles database operations for multi-tenancy infrastructure.
"""

import os
import json
from datetime import datetime
from wo.core.logging import Log
from wo.core.database import db_session
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


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
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MTDatabase:
    """Multi-tenancy database operations"""
    
    @staticmethod
    def initialize_tables(app):
        """Create multi-tenancy tables if they don't exist"""
        try:
            # Get database path from WordOps config
            db_path = '/var/lib/wo/wordops.db'
            
            # Create tables using SQLAlchemy
            engine = create_engine(f'sqlite:///{db_path}')
            Base.metadata.create_all(engine)
            
            Log.debug(app, "Multi-tenancy database tables initialized")
            
        except Exception as e:
            Log.debug(app, f"Failed to initialize multi-tenancy tables: {e}")
    
    @staticmethod
    def is_initialized(app):
        """Check if multi-tenancy is initialized"""
        try:
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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
            with db_session() as session:
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