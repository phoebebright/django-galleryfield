from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import BaseValidator
from django.db import models
from django.db.models import Case, IntegerField, Value, When
from django.db.models.query_utils import DeferredAttribute
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy

from gallery_widget import conf
from gallery_widget import defaults as _defaults
from gallery_widget.utils import apps, get_or_check_image_field, logger
from gallery_widget.widgets import GalleryWidget


@deconstructible
class MaxNumberOfImageValidator(BaseValidator):
    message = ngettext_lazy(
        'Number of images exceeded, only %(limit_value)d allowed',
        'Number of images exceeded, only %(limit_value)d allowed',
        'limit_value')
    code = 'max_number_of_images'

    def compare(self, a, b):
        return a > b

    def clean(self, x):
        return len(x)


class GalleryDescriptor(DeferredAttribute):
    """
    Used django.db.models.fields.files.FileDescriptor as an example.
    """

    def __set__(self, instance, value):
        instance.__dict__[self.field.attname] = value

    def __get__(self, instance, cls=None):
        image_list = super().__get__(instance, cls)

        if not isinstance(image_list, GalleryImages):
            attr = self.field.attr_class(instance, self.field, image_list)
            instance.__dict__[self.field.name] = attr

        return instance.__dict__[self.field.name]


class GalleryImages(list):
    def __init__(self, instance, field, field_value):
        # When field_value is None,
        # (This happens when the GalleryField was saved as null)
        field_value = field_value or []
        super().__init__(field_value)
        self._field = field
        self.instance = instance
        self._value = field_value or []

    @property
    def objects(self):
        model = apps.get_model(self._field.target_model)

        # Preserving the order of image using id__in=pks
        # https://stackoverflow.com/a/37146498/3437454
        cases = [When(id=x, then=Value(i)) for i, x in enumerate(self._value)]
        case = Case(*cases, output_field=IntegerField())
        filter_kwargs = {"id__in": self._value}
        queryset = model.objects.filter(**filter_kwargs)
        queryset = queryset.annotate(_order=case).order_by('_order')
        return queryset


class GalleryField(models.JSONField):
    """This is a model field which saves the id of images.

    :param target_model: A string in the form of ``"app_label.model_name"``,
           which can be loaded by :meth:`django.apps.get_model` (see 
           `Django docs <https://docs.djangoproject.com/en/dev/ref/applications/#django.apps.apps.get_model>`_),
           defaults to `None`. If `None`, ``gallery_widget.BuiltInGalleryImage``,
           which can be overridden by 
           ``settings.DJANGO_GALLERY_WIDGET_CONFIG["default_target_image_model"]``,
           will be used.

    :type target_model: str, optional.

    A valid ``target_model`` need to meet one of the following 2 requirements:

    1. It has a :class:`django.db.models.ImageField` named ``image`` 

    2. It has a :class:`django.db.models.ImageField` which not named ``image``
       but the field can be accessed by a `classmethod` :meth:`get_image_field`,
       for example:

    .. code-block:: python
        
       class MyImage(models.Model):
            photo = models.ImageField(
                upload_to="my_images", storage=default_storage, verbose_name=_("Image"))
            creator = models.ForeignKey(
                    settings.AUTH_USER_MODEL, null=False, blank=False,
                            verbose_name=_('Creator'), on_delete=models.CASCADE)
            
            @classmethod
            def get_image_field(cls):
                return cls._meta.get_field("photo")

    .. note:: As demonstrated in above example, when defining the :meth:`get_image_field`,
       we can't simply ``return cls.photo`` because it
       returns a :class:`django.db.models.fields.files.ImageFieldFile`
       object instead of a :class:`django.db.models.ImageField` object.
 

    """  # noqa

    attr_class = GalleryImages
    descriptor_class = GalleryDescriptor

    def contribute_to_class(self, cls, name, private_only=False):
        super().contribute_to_class(cls, name, private_only)
        setattr(cls, self.attname, self.descriptor_class(self))

    def __init__(self, target_model=None, *args, **kwargs):
        self._init_target_model = self.target_model = target_model
        if target_model is None:
            self.target_model = _defaults.DEFAULT_TARGET_IMAGE_MODEL

        self.target_model_image_field = (
            self._get_image_field_or_test(is_checking=False))

        super().__init__(*args, **kwargs)

    def _get_image_field_or_test(self, is_checking=False):
        return get_or_check_image_field(
            obj=self,
            target_model=self._init_target_model,
            check_id_prefix="gallery_field",
            is_checking=is_checking)

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        errors.extend(self._check_target_model())
        return errors

    def _check_target_model(self):
        return self._get_image_field_or_test(is_checking=True)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['target_model'] = self.target_model
        return name, path, args, kwargs

    def formfield(self, **kwargs):
        defaults = ({
            "required": True,

            # The following 2 are used to validate GalleryWidget params
            # see GalleryWidget.defaults_checks()
            "target_model": self.target_model,
            "model_field": self.__class__.__name__
        })
        defaults.update(kwargs)
        formfield = super().formfield(**{
            'form_class': GalleryFormField,
            **defaults,
        })
        return formfield


class GalleryFormField(forms.JSONField):
    """The default formfield for :class:`gallery_widget.fields.GalleryField`.

    :param max_number_of_images: Max allowed number of images, defaults
           to `None`, which means unlimited.
    :type max_number_of_images: int, optional.

    :param kwargs: Besides the options from parent class, the following were added:

           * target_model: str, a valid target image model which can be loaded by
             ``apps.get_model``. When this field is used in the model form,
             it is auto configured by the model instance.

             However, if this field is used as a non-model form field, when
             not specified, it will use the built-in default target image
             model ``gallery_widget.BuiltInGalleryImage``.

           * widget: if not specified, defaults to ``GalleryWidget`` with default
             values.


    .. note:: If `target_model` not specified when initializing, an info will
       be logged to the stdout, which can be turned off by adding
       ``gallery_form_field.I001`` in ``settings.SILENCED_SYSTEM_CHECKS``.

    """

    default_error_messages = {
        'required': _("The submitted file is empty."),
        'invalid': _("The submitted images are invalid."),
    }

    def __init__(self, max_number_of_images=None, **kwargs):

        # The following 2 are used to validate GalleryWidget params
        # see GalleryWidget.defaults_checks()
        self._image_model = kwargs.pop("target_model", None)
        image_model_not_configured = False
        if self._image_model is None:
            # This happens when the formfield is used in a Non-model form
            image_model_not_configured = True
            self._image_model = _defaults.DEFAULT_TARGET_IMAGE_MODEL

        # Make sure the model is valid target image model
        if (self._image_model != _defaults.DEFAULT_TARGET_IMAGE_MODEL
                or image_model_not_configured):
            errors = get_or_check_image_field(
                obj=self,
                target_model=(
                    None if image_model_not_configured else self._image_model),
                check_id_prefix="gallery_form_field",
                is_checking=True)
            for error in errors:
                if error.is_serious():
                    raise ImproperlyConfigured(str(error))
                else:
                    if error.is_silenced():
                        continue
                    logger.info(str(error))

        # This is used for widget to identify which object the widget is servicing.
        # That information will be used when raising errors.
        self._widget_is_servicing = (
                kwargs.pop("model_field", None) or self.__class__.__name__)

        self._max_number_of_images = max_number_of_images
        super().__init__(**kwargs)

    _widget = GalleryWidget

    @property
    def widget(self):
        return self._widget

    @widget.setter
    def widget(self, value):
        # Property and setter are used to make sure the attributes will
        # be passed to new widget instance when the widget instance
        # is changed.
        setattr(value, "max_number_of_images", self.max_number_of_images)
        setattr(value, "image_model", self._image_model)
        setattr(value, "widget_is_servicing", self._widget_is_servicing)

        # Re-initialize the widget
        value.is_localized = bool(self.localize)
        value.is_required = self.required
        extra_attrs = self.widget_attrs(value) or {}
        value.attrs.update(extra_attrs)
        self._widget = value

        if not isinstance(self.widget, GalleryWidget):
            return

        # We set the upload_handler_url and fetch_request_url
        # when the widget didn't specify them.
        self._set_widget_upload_handler_url()
        self._set_widget_fetch_request_url()

    @property
    def _target_model_name(self):
        return self._image_model.split(".")[-1]

    def _set_widget_upload_handler_url(self):
        if self.widget.upload_handler_url:
            return

        # Here we required a target_model should have a upload_handler_url
        # name in url_conf in the form of modelname-upload in lower case
        self.widget.upload_handler_url = (
                "%s-upload" % self._target_model_name.lower())

    def _set_widget_fetch_request_url(self):
        if self.widget.disable_fetch or self.widget.fetch_request_url:
            return

        # Here we required a target_model should have a fetch_request_url
        # name in url_conf in the form of modelname-fetch in lower case
        self.widget.fetch_request_url = (
                "%s-fetch" % self._target_model_name.lower())

    @property
    def max_number_of_images(self):
        return self._max_number_of_images

    @max_number_of_images.setter
    def max_number_of_images(self, value):
        if value is not None:
            if not str(value).isdigit():
                raise TypeError(
                    "'max_number_of_images' expects a positive integer, "
                    "got %s." % str(value))
            value = int(value)
        self._max_number_of_images = value
        self._widget.max_number_of_images = value

        if value:
            self.validators.append(
                MaxNumberOfImageValidator(int(value)))

    def widget_attrs(self, widget):
        # If BootStrap is loaded, "hiddeninput" is added by BootStrap.
        # However, we need that css class to check changes of the form,
        # so we added it manually.
        return {
            "class": " ".join(
                [conf.FILES_FIELD_CLASS_NAME, "hiddeninput"])
        }

    def to_python(self, value):
        converted = super().to_python(value)

        if converted in self.empty_values:
            return converted

        # Make sure the json is a list of pks
        if not isinstance(converted, list):
            raise ValidationError(
                self.error_messages['invalid'],
                code='invalid',
                params={'value': converted},
            )

        for _pk in converted:
            if not str(_pk).isdigit():
                raise ValidationError(
                    self.error_messages['invalid'],
                    code='invalid',
                    params={'value': converted},
                )

        # Make sure all pks exists
        image_model = apps.get_model(self._image_model)
        if (image_model.objects.filter(
                pk__in=list(map(int, converted))).count()
                != len(converted)):
            converted_copy = converted[:]
            converted = []
            for pk in converted_copy:
                if image_model.objects.filter(pk=pk).count():
                    converted.append(pk)

        return converted
