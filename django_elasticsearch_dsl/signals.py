# encoding: utf-8
"""
A convenient way to attach django-elasticsearch-dsl to Django's signals and
cause things to index.
"""

from __future__ import absolute_import

from celery.exceptions import ImproperlyConfigured
from django.db import models
from django.apps import apps

from .registries import registry


class BaseSignalProcessor(object):
    """Base signal processor.

    By default, does nothing with signals but provides underlying
    functionality.
    """

    def __init__(self, connections):
        self.connections = connections
        self.setup()

    def setup(self):
        """Set up.

        A hook for setting up anything necessary for
        ``handle_save/handle_delete`` to be executed.

        Default behavior is to do nothing (``pass``).
        """
        # Do nothing.

    def teardown(self):
        """Tear-down.

        A hook for tearing down anything necessary for
        ``handle_save/handle_delete`` to no longer be executed.

        Default behavior is to do nothing (``pass``).
        """
        # Do nothing.

    def handle_m2m_changed(self, sender, instance, action, **kwargs):
        if action in ('post_add', 'post_remove', 'post_clear'):
            self.handle_save(sender, instance)
        elif action in ('pre_remove', 'pre_clear'):
            self.handle_pre_delete(sender, instance)

    def handle_save(self, sender, instance, **kwargs):
        """Handle save.

        Given an individual model instance, update the object in the index.
        Update the related objects either.
        """
        registry.update(instance)
        registry.update_related(instance)

    def handle_pre_delete(self, sender, instance, **kwargs):
        """Handle removing of instance object from related models instance.
        We need to do this before the real delete otherwise the relation
        doesn't exists anymore and we can't get the related models instance.
        """
        registry.delete_related(instance)

    def handle_delete(self, sender, instance, **kwargs):
        """Handle delete.

        Given an individual model instance, delete the object from index.
        """
        registry.delete(instance, raise_on_error=False)


class DjangoSignalsMixin(object):
    """
    Enable Django signals integration
    """
    def setup(self):
        # Listen to all model saves.
        models.signals.post_save.connect(self.handle_save)
        models.signals.post_delete.connect(self.handle_delete)

        # Use to manage related objects update
        models.signals.m2m_changed.connect(self.handle_m2m_changed)
        models.signals.pre_delete.connect(self.handle_pre_delete)

    def teardown(self):
        # Listen to all model saves.
        models.signals.post_save.disconnect(self.handle_save)
        models.signals.post_delete.disconnect(self.handle_delete)
        models.signals.m2m_changed.disconnect(self.handle_m2m_changed)
        models.signals.pre_delete.disconnect(self.handle_pre_delete)


class RealTimeSignalProcessor(DjangoSignalsMixin, BaseSignalProcessor):
    """Real-time signal processor.

    Allows for observing when saves/deletes fire and automatically updates the
    search engine appropriately.
    """
    pass

try:
    from celery import shared_task
except ImportError:
    pass
else:
    class CelerySignalProcessor(DjangoSignalsMixin, BaseSignalProcessor):
        """Celery signal processor.

        Allows automatic updates on the index as delayed background tasks using
        Celery.

        NB: We cannot process deletes as background tasks.
        By the time the Celery worker would pick up the delete job, the
        model instance would already deleted. We can get around this by
        setting Celery to use `pickle` and sending the object to the worker,
        but using `pickle` opens the application up to security concerns.
        """

        def handle_save(self, sender, instance, **kwargs):
            """Handle save with a Celery task.

            Given an individual model instance, update the object in the index.
            Update the related objects either.
            """
            pk = instance.pk
            app_label = instance._meta.app_label
            model_name = instance._meta.concrete_model.__name__

            if instance._meta.concrete_model not in registry:
                self.registry_update_task.delay(pk, app_label, model_name)
                self.registry_update_related_task.delay(pk, app_label, model_name)

        @shared_task()
        def registry_update_task(pk, app_label, model_name):
            """Handle the update on the registry as a Celery task."""
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                pass
            else:
                registry.update(
                    model.objects.get(pk=pk)
                )

        @shared_task()
        def registry_update_related_task(pk, app_label, model_name):
            """Handle the related update on the registry as a Celery task."""
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                pass
            else:
                registry.update_related(
                    model.objects.get(pk=pk)
                )
